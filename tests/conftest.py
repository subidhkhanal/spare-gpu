import json
import sys
import uuid
from datetime import time

import httpx
import pytest

from gpu_token_utilization import keystore
from gpu_token_utilization.keystore import TARGET_ENV
from gpu_token_utilization.client import BharatCodeClient, RetryPolicy
from gpu_token_utilization.fakeapi import FakeBharatCode
from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.store import Store

KEY = "bc_live_fake_key"


@pytest.fixture(autouse=True)
def no_real_api_key(monkeypatch):
    """No test may find the user's real key (and so reach the real API, or let a background
    runner do it). Tests that need a key set a fake one."""
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_real_desktop(monkeypatch):
    """No test may find a real BharatCode Desktop server (pytest run from Desktop's terminal
    would inherit its password). Tests that need one use fakedesktop."""
    for name in ("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME", "GTU_DESKTOP_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_bharatcode_config(monkeypatch, tmp_path):
    """No test may read the user's real BharatCode config (jobs read it to switch its MCP servers
    off). Tests that need one write it under GTU_BHARATCODE_CONFIG_DIR or this APPDATA."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


@pytest.fixture(autouse=True)
def fake_bharatcode_cli(monkeypatch):
    """No test ever starts the real BharatCode CLI; tests about a missing CLI delete this."""
    monkeypatch.setenv("GTU_BHARATCODE_CMD", json.dumps([sys.executable, "-m", "gpu_token_utilization.fakecli"]))


@pytest.fixture(autouse=True)
def neutral_routing_policy(tmp_path_factory, monkeypatch):
    """Tests never depend on the shipped routing_policy.json, which each model evaluation
    rewrites: nothing is switch-safe unless a test says so."""
    path = tmp_path_factory.getbasetemp() / "routing_policy.json"
    if not path.exists():
        path.write_text(json.dumps({"version": 1, "types": {k: "unmeasured" for k in ("tests", "docs", "review")}}),
                        encoding="utf-8")
    monkeypatch.setenv("GTU_ROUTING_POLICY", str(path))


@pytest.fixture(autouse=True)
def private_credential_target(monkeypatch):
    """Every test gets its own Credential Manager entry name, so no test can read (or
    overwrite) a key the user saved with `gtu setkey`. Tests that save one clean up."""
    target = f"spare-gpu-test/{uuid.uuid4().hex}"
    monkeypatch.setenv(TARGET_ENV, target)
    yield target
    try:
        keystore.delete()
    except keystore.KeystoreError:
        pass


@pytest.fixture
def fake():
    return FakeBharatCode(api_key=KEY)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".gtu" / "gtu.db") as s:
        yield s


@pytest.fixture
def job_repo(tmp_path):
    """A repo path for jobs that never touch git. Must be a real temp folder: the scheduler
    writes per-job state under <repo>/.gtu, and a literal like "/repo" escapes the sandbox."""
    return str(tmp_path / "repo")


@pytest.fixture
def client(fake, store):
    # Scheduler-style client: no retries, the gate decides when to try again.
    return BharatCodeClient(
        api_key=KEY, store=store, transport=fake.transport(), retry=RetryPolicy(max_retries=0),
        sleep=fake.clock.sleep, clock=fake.clock, rng=lambda: 0.5,  # 0.5 = no jitter
    )


def git(cwd, *args):
    """Run git in a test repo; identity is passed in so tests work without a global git config."""
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """A small committed repo on branch main."""
    repo = tmp_path / "sample"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def make_gate(fake, store, client):
    def make(probe=True, **overrides):
        # Tests run at any hour, so the fallback window is all day unless a test says otherwise.
        cfg = GateConfig(**{"window": (time(0, 0), time(0, 0)), **overrides})
        return Gate(
            store, cfg, http=httpx.Client(transport=fake.transport()),
            client=client if probe else None, now=fake.clock.utcnow,
        )

    return make


# What `bharatcode stats --models` prints (its MODEL USAGE box), for the stats and pack_cost tests.
STATS = """┌────────────────────────────────────────────────────────┐
│                      MODEL USAGE                       │
├────────────────────────────────────────────────────────┤
│ bharatcode/deepseek-v4.1-flash                         │
│   Messages                                         12 │
│   Input Tokens                                  120.5K │
│   Output Tokens                                   8.2K │
│   Cache Read                                     40.0K │
│   Cache Write                                       0 │
│   Cost                                        $0.0000 │
├────────────────────────────────────────────────────────┤
│ bharatcode/qwen-3.8-27b                                │
│   Messages                                         30 │
│   Input Tokens                                  300.0K │
│   Output Tokens                                  20.0K │
│   Cache Read                                        0 │
│   Cache Write                                       0 │
│   Cost                                        $0.0000 │
└────────────────────────────────────────────────────────┘
"""

