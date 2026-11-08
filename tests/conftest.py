import json
import uuid

import pytest

from gpu_token_utilization import keystore
from gpu_token_utilization.keystore import TARGET_ENV
from gpu_token_utilization import workers
from gpu_token_utilization.client import BharatCodeClient, RetryPolicy
from gpu_token_utilization.fakeapi import FakeBharatCode
from gpu_token_utilization.fakedesktop import FakeDesktop, Step
from gpu_token_utilization.store import Store

KEY = "bc_live_fake_key"


@pytest.fixture(autouse=True)
def no_real_api_key(monkeypatch):
    """No test may find the user's real key (and so reach the real API, or let a background
    runner do it). Tests that need a key set a fake one."""
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_real_desktop(monkeypatch, tmp_path):
    """No test may find a real BharatCode Desktop server (pytest run from Desktop's terminal
    would inherit its password). Tests that need one use fakedesktop."""
    for name in ("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME", "GTU_DESKTOP_URL", "XDG_STATE_HOME",
                 "GTU_DESKTOP_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))  # no real Desktop session data either


@pytest.fixture(autouse=True)
def no_real_bharatcode_config(monkeypatch, tmp_path):
    """No test may read the user's real BharatCode config (jobs read it to switch its MCP servers
    off). Tests that need one write it under GTU_BHARATCODE_CONFIG_DIR or this APPDATA."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


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
    # Scheduler-style client: no retries; the scheduler waits out a busy answer and tries the step again.
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
def desktop_fake():
    """A fake BharatCode Desktop server; desktop_fake.agent() is a DesktopAgent talking to it."""
    return FakeDesktop()


class QuickAgent:
    """Stands in for DesktopAgent in tests whose plans never call it: only the model matters."""
    label = "BharatCode Desktop"
    timeout = 60

    def __init__(self, model="bharatcode/deepseek-v4.1-flash"):
        self.model = model

    def with_model(self, model):
        return QuickAgent(model)


def quick_worker(store=None, log=None, config=None):
    """What workers.build returns inside Desktop, without a Desktop."""
    return workers.Worker(client=None, agent=QuickAgent(), desktop_missing=None, inside_desktop=True)


def quick_plans(write=None):
    """Plans whose steps finish at once; `write(ctx)` may put a file in the job's copy."""
    def step(ctx):
        if write:
            write(ctx)
        return "wrote it"

    return {kind: [("write", step), ("verify", lambda ctx: "Kept 1 passing test(s), dropped 0.")]
            for kind in ("tests", "docs", "review", "fix")}


def quick_tools(base, http, **kwargs):
    """SpareTools whose delegated jobs run at once against the fakes (no Desktop, no waiting)."""
    from gpu_token_utilization.mcp_server import SpareTools

    kwargs.setdefault("make_worker", quick_worker)
    kwargs.setdefault("plans", quick_plans())
    kwargs.setdefault("sleep", lambda seconds: None)
    return SpareTools(base, http=http, **kwargs)


@pytest.fixture
def scenario(desktop_fake):
    """Script the fake Desktop's sessions: each step's tokens, the files it writes and how many
    polls it takes, then an error or the closing text. `calls` gives each session in turn its own
    list of steps (the last list repeats). Returns the fake, whose .sessions record each session's
    folder, prompt and permission file."""
    def steps_of(given):
        made = [Step(tokens=s.get("tokens", 1000), writes=s.get("writes") or {}, polls=s.get("polls", 1))
                for s in given or [{"tokens": 1000}]]
        if not error_now[0]:
            made[-1].text = text_now[0]
        return made

    error_now, text_now = [None], ["done"]

    def script(steps=None, error=None, text="done", calls=None):
        error_now[0], text_now[0] = error, text
        desktop_fake.steps = steps_of(steps)
        desktop_fake.calls = [steps_of(c) for c in calls] if calls else None
        desktop_fake.error = error
        return desktop_fake

    return script


def desktop_db(path, replies):
    """A BharatCode Desktop session database with the tables stats.tokens_since reads. replies:
    (folder, ms since epoch, model, {"input", "output", "reasoning", "cache": {"read", "write"}, "total"})."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table session (id text primary key, directory text)")
    db.execute("create table message (id text primary key, session_id text, time_created integer, data text)")
    sessions = {}
    for n, (folder, at, model, tokens) in enumerate(replies, 1):
        sid = sessions.setdefault(folder, f"ses_{len(sessions) + 1}")
        db.execute("insert or ignore into session values (?, ?)", (sid, folder))
        provider, _, model_id = model.partition("/")
        data = {"role": "assistant", "providerID": provider, "modelID": model_id, "tokens": tokens}
        db.execute("insert into message values (?, ?, ?, ?)", (f"msg_{n}", sid, at, json.dumps(data)))
        db.execute("insert into message values (?, ?, ?, ?)", (f"msg_{n}_user", sid, at, json.dumps({"role": "user"})))
    db.commit()
    db.close()
    return path
