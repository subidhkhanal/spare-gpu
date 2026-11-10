"""The fix job end to end: real git repo, fake BharatCode Desktop, real pytest."""

import sys
from pathlib import Path

import pytest

from gpu_token_utilization import jobs, landing, worktree
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.fakedesktop import FakeClock, Step
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import BUSY_WAIT, Scheduler, kept_nothing

from conftest import git

# Generated test files put the repo root on sys.path, as a real tests folder would via conftest.
IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
DOCUMENTED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def only_session(desktop):
    (session,) = desktop.sessions.values()
    return session


@pytest.fixture
def run_jobs(store, clock, desktop_fake):
    def run(job, budgets=None, config=None, agent=None, **kwargs):
        logs = []
        scheduler = Scheduler(
            store,
            agent=agent or desktop_fake.agent(timeout=60),
            budgets=budgets or Budgets(),
            config=config or JobConfig(test_command=(sys.executable, "-m", "pytest")),
            sleep=clock.sleep, now=clock.utcnow, log=logs.append, **kwargs,
        )
        scheduler.run_job(job.id)
        return logs

    return run


# The project's own test command, allowed whatever the interpreter is called (python3 on Linux).
FIX_CONFIG = JobConfig(test_command=(sys.executable, "-m", "pytest"),
                       allowed_commands=("python -m pytest", "python3 -m pytest", "pytest"))
EXISTING_TEST = IMPORT_SRC + "def test_adds_numbers():\n    assert add(2, 3) == 5\n"
FINDINGS = "src/calc.py:2 the function has no docstring; add one"


@pytest.fixture
def repo_with_tests(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_calc.py").write_text(EXISTING_TEST)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "tests")
    return git_repo


def result_files(store, job_id):
    """The files a finished job left in its spare-gpu/<date>_<time>/ folder (empty if none)."""
    job = store.get_job(job_id)
    if not job.output_dir:
        return set()
    return set(landing.exported_files(Path(job.repo) / job.output_dir))  # RUN.md and manifest.json sit next to them


def result(store, job_id, path):
    job = store.get_job(job_id)
    return (Path(job.repo) / job.output_dir / path).read_text(encoding="utf-8")


def assert_main_untouched(repo, head):
    """Same commit, same branch, nothing changed, and no branch or copy of the job left behind."""
    assert git(repo, "rev-parse", "main") == head
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "branch", "--list", "gtu/*") == ""
    assert git(repo, "status", "--porcelain") == ""
    worktrees = Path(repo) / ".gtu" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


# --- what every job's session gets ----------------------------------------------------------

def test_session_tokens_are_logged_per_step(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 11500, "writes": {"src/calc.py": DOCUMENTED}}, {"tokens": 300}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(job, config=FIX_CONFIG)
    rows = [r for r in store.requests(job.id) if r["endpoint"] == "bharatcode-desktop"]
    assert [r["total_tokens"] for r in rows] == [11500, 300]


def test_the_session_is_given_the_test_allowlist_and_the_worktree(repo_with_tests, store, scenario, run_jobs):
    desktop = scenario(steps=[{"tokens": 100}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(job, config=FIX_CONFIG)
    session = only_session(desktop)
    assert Path(session["directory"]) == worktree.worktree_path(repo_with_tests, job.id)
    bash = session["config"]["permission"]["bash"]
    assert list(bash)[0] == "*" and bash["*"] == "deny" and bash["python -m pytest*"] == "allow"


def test_crash_during_verify_resumes_without_rerunning_the_session(repo_with_tests, store, scenario, run_jobs, monkeypatch):
    desktop = scenario(steps=[{"tokens": 11500, "writes": {"src/calc.py": DOCUMENTED}}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    real = jobs.run_test_command
    crashes = {"left": 1}

    def crash_once(ctx):
        if crashes["left"]:
            crashes["left"] -= 1
            raise KeyboardInterrupt  # the process is killed mid-verify
        return real(ctx)

    monkeypatch.setattr(jobs, "run_test_command", crash_once)
    with pytest.raises(KeyboardInterrupt):
        run_jobs(job, config=FIX_CONFIG)
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running"]

    run_jobs(job, config=FIX_CONFIG)  # a fresh start
    assert store.get_job(job.id).status == "done"
    assert len(desktop.sessions) == 1  # the session ran once, its work was kept in the patch
    assert "Return the sum" in result(store, job.id, "src/calc.py")


def test_a_failed_session_fails_the_job_with_its_message(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 100}], error="Unexpected server error")
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    logs = run_jobs(job, config=FIX_CONFIG)
    assert store.get_job(job.id).status == "failed"
    assert any("Unexpected server error" in line for line in logs)


# --- fix job ------------------------------------------------------------------------------

def test_fix_job_keeps_the_fix_when_the_tests_pass_and_never_lets_it_touch_existing_tests(repo_with_tests, store, scenario, run_jobs):
    head = git(repo_with_tests, "rev-parse", "HEAD")
    desktop = scenario(steps=[{"tokens": 3000, "writes": {
        "src/calc.py": 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n',
        "tests/test_calc.py": "def test_nothing():\n    pass\n",  # a fix that weakens the tests
    }}, {"tokens": 200}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(job, config=FIX_CONFIG)
    assert store.get_job(job.id).status == "done"
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Kept the fix in 1 file(s): the test command passed.")
    assert "- kept src/calc.py" in verify and "- reverted tests/test_calc.py: a fix may not change existing tests" in verify
    assert result_files(store, job.id) == {"src/calc.py"}
    assert "Return the sum" in result(store, job.id, "src/calc.py")
    prompt = only_session(desktop)["prompt"]["parts"][0]["text"]
    assert FINDINGS in prompt and "Do not modify or delete existing test files" in prompt
    assert_main_untouched(repo_with_tests, head)


def test_fix_job_drops_the_fix_when_the_tests_fail(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 3000, "writes": {"src/calc.py": "def add(a, b):\n    return a - b\n"}}])
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(job, config=FIX_CONFIG)
    assert store.get_job(job.id).status == "done"
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Kept the fix in 0 file(s): the test command failed with it.")
    assert "discarded the fix" in verify
    assert kept_nothing(verify)  # the router's fallback sees it as nothing kept
    assert result_files(store, job.id) == set() and store.get_job(job.id).output_dir is None


def test_fix_job_without_findings_or_without_a_fix_finishes_honestly(repo_with_tests, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 500}], text="Nothing to change.")
    nothing = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    run_jobs(nothing, config=FIX_CONFIG)
    assert store.get_job(nothing.id).status == "done" and "No fix was generated" in store.steps(nothing.id)[1]["output"]
    blank = store.add_job("fix", ".", str(repo_with_tests))
    run_jobs(blank, config=FIX_CONFIG)
    assert store.get_job(blank.id).status == "failed"
    assert "needs the findings" in (store.steps(blank.id)[0]["output"] or "")


def test_a_session_that_times_out_is_retried_after_a_wait(repo_with_tests, store, desktop_fake, clock, run_jobs):
    # the first session never finishes within the agent's timeout; the second answers at once
    slow = [Step(tokens=100, polls=1000, text="never seen")]
    fast = [Step(tokens=200, text="Nothing to change.")]
    desktop_fake.calls = [slow, fast]
    polls = FakeClock()  # the agent's own time: each poll moves it on
    agent = desktop_fake.agent(timeout=60, sleep=polls.sleep, clock=polls)
    job = store.add_job("fix", ".", str(repo_with_tests), note=FINDINGS)
    logs = run_jobs(job, agent=agent, config=FIX_CONFIG)

    assert store.get_job(job.id).status == "done", "\n".join(logs)
    assert len(desktop_fake.sessions) == 2
    assert desktop_fake.sessions["ses_1"]["aborted"] and not desktop_fake.sessions["ses_2"]["aborted"]
    assert clock.sleeps == [BUSY_WAIT]  # waited once before trying the step again
    assert any("busy" in line for line in logs)
    assert "No fix was generated" in store.steps(job.id)[1]["output"]


# --- budgets -----------------------------------------------------------------------------

def test_job_budget_stops_the_session_and_pauses_the_job(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 2000, "sleep": 0.3, "writes": {"src/calc.py": DOCUMENTED}} for _ in range(6)])
    job = store.add_job("fix", ".", str(git_repo), note=FINDINGS)
    logs = run_jobs(job, budgets=Budgets(per_job=5000), config=FIX_CONFIG)
    assert store.get_job(job.id).status == "paused"
    assert [s["status"] for s in store.steps(job.id)] == ["pending", "pending"]
    used = store.request_summary(job.id)["total_tokens"]
    assert 5000 <= used <= 7000
    # nothing half-done was kept: the paused job's copy has nothing saved beyond its starting point
    assert worktree.changed_files(worktree.worktree_path(git_repo, job.id), None) == []
    assert any("budget" in line for line in logs)


def test_budget_settings_from_env(monkeypatch):
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "1234")
    assert Budgets.from_env().per_job == 1234


# --- config --------------------------------------------------------------------------------

def test_test_command_must_be_allowlisted(monkeypatch):
    monkeypatch.setenv("GTU_TEST_COMMAND", "bash -c 'rm -rf /'")
    with pytest.raises(ValueError, match="not in the allowlist"):
        JobConfig.from_env()


@pytest.mark.parametrize("command, ok", [
    (("python", "-m", "pytest"), True),
    ((sys.executable, "-m", "pytest"), True),
    (("pytest", "-x"), True),
    (("py", "-m", "pytest"), True),  # the Windows launcher
    (("python3", "-m", "pytest"), True),
    (("C:\\venv\\Scripts\\python3.12.exe", "-m", "pytest"), True),
    (("pythonw", "-m", "pytest"), True),
    (("npm", "test"), True),
    (("npm", "install"), False),
    (("python", "evil.py"), False),
    (("pypy", "-m", "pytest"), False),
])
def test_command_allowlist(command, ok):
    assert jobs.command_allowed(command, jobs.DEFAULT_ALLOWED) is ok
