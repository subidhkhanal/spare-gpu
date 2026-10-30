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
