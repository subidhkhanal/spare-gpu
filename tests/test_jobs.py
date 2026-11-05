"""The three job types end to end: real git repo, fake BharatCode CLI, fake API, real pytest."""

import json
import sys

import pytest

from gpu_token_utilization import jobs
from gpu_token_utilization.agent_cli import BharatCodeCli
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler
from gpu_token_utilization.store import RequestLog

from conftest import git

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]

# Generated test files put the repo root on sys.path, as a real tests folder would via conftest.
IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
GOOD_AND_BAD_TESTS = IMPORT_SRC + (
    "def test_adds_numbers():\n    assert add(2, 3) == 5\n\n\n"
    "def test_adds_negatives():\n    assert add(-2, -3) == -5\n\n\n"
    "def test_wrong_on_purpose():\n    assert add(2, 2) == 5\n"
)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / "scenario.json"
    record = tmp_path / "cli-calls.jsonl"
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(path))

    def write(**data):
        data.setdefault("record", str(record))
        path.write_text(json.dumps(data))
        return record

    return write


@pytest.fixture
def run_jobs(store, client, make_gate, fake):
    def run(budgets=None, **kwargs):
        logs = []
        scheduler = Scheduler(
            store, make_gate(), client,
            cli=BharatCodeCli(command=FAKE_CLI, timeout=60),
            budgets=budgets or Budgets(),
            config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
            sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append, **kwargs,
        )
        scheduler.run()
        return logs

    return run


def branch_files(repo, job_id):
    return set(git(repo, "ls-tree", "--name-only", "-r", f"gtu/{job_id}").splitlines())


def assert_main_untouched(repo, head):
    assert git(repo, "rev-parse", "main") == head
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""


# --- tests job ---------------------------------------------------------------------------

def test_tests_job_keeps_only_passing_tests(git_repo, store, scenario, run_jobs):
    head = git(git_repo, "rev-parse", "HEAD")
    scenario(steps=[
        {"tokens": 11500, "writes": {
            "tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS,
            "tests/test_gtu_broken.py": "def test_x(:\n    pass\n",  # does not even parse
            "src/calc.py": "def add(a, b):\n    return a - b\n",  # sneaky source change
        }},
        {"tokens": 400},
    ])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    files = branch_files(git_repo, job.id)
    assert "tests/test_gtu_calc.py" in files and "tests/test_gtu_broken.py" not in files
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)  # test runs leave no caches behind
    kept = git(git_repo, "show", f"gtu/{job.id}:tests/test_gtu_calc.py")
    assert "test_adds_numbers" in kept and "test_adds_negatives" in kept
    assert "test_wrong_on_purpose" not in kept
    assert git(git_repo, "show", f"gtu/{job.id}:src/calc.py") == "def add(a, b):\n    return a + b"
    # one commit, made by the verify step; the unchecked CLI output was never committed
    subjects = git(git_repo, "log", "--format=%s", f"main..gtu/{job.id}").splitlines()
    assert subjects == [f"gtu tests src/calc.py: verify (job {job.id})"]
    verify = store.steps(job.id)[1]["output"]
    assert "Kept 2 passing test(s), dropped 1" in verify and "reverted src/calc.py" in verify
    assert_main_untouched(git_repo, head)


def test_tests_job_cli_tokens_are_logged_per_step(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": IMPORT_SRC + "def test_ok():\n    assert add(1, 1) == 2\n"}},
                    {"tokens": 300}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    rows = [r for r in store.requests(job.id) if r["endpoint"] == "bharatcode-cli"]
    assert [r["total_tokens"] for r in rows] == [11500, 300]
    assert all(r["gate_source"] == "livestats" for r in rows)


def test_tests_job_with_no_tests_generated_finishes_cleanly(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 9000}], text="I could not write tests.")
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    assert store.get_job(job.id).status == "done"
    assert "nothing to keep" in store.steps(job.id)[1]["output"]
    assert git(git_repo, "log", "--format=%s", f"main..gtu/{job.id}") == ""


def test_cli_is_given_the_test_allowlist_and_the_worktree(git_repo, store, scenario, run_jobs):
    record = scenario(steps=[{"tokens": 100}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs()
    call = json.loads(record.read_text().splitlines()[0])
    assert call["args"][call["args"].index("--dir") + 1].endswith(f"worktrees\\{job.id}") or \
        call["args"][call["args"].index("--dir") + 1].endswith(f"worktrees/{job.id}")
    bash = call["config"]["permission"]["bash"]
    assert list(bash)[0] == "*" and bash["*"] == "deny" and bash["python -m pytest*"] == "allow"


def test_crash_during_verify_resumes_without_rerunning_the_cli(git_repo, store, scenario, run_jobs, monkeypatch):
    record = scenario(steps=[{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": GOOD_AND_BAD_TESTS}}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    real = jobs._run_pytest
    crashes = {"left": 1}

    def crash_once(ctx, path):
        if crashes["left"]:
            crashes["left"] -= 1
            raise KeyboardInterrupt  # the process is killed mid-verify
        return real(ctx, path)

    monkeypatch.setattr(jobs, "_run_pytest", crash_once)
    with pytest.raises(KeyboardInterrupt):
        run_jobs()
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running"]

    run_jobs()  # a fresh start
    assert store.get_job(job.id).status == "done"
    assert len(record.read_text().splitlines()) == 1  # the CLI ran once, its work was kept in the patch
    kept = git(git_repo, "show", f"gtu/{job.id}:tests/test_gtu_calc.py")
    assert "test_adds_numbers" in kept and "test_wrong_on_purpose" not in kept


def test_cli_touching_the_main_checkout_stops_the_job(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 100, "writes": {"../../../src/calc.py": "def add(a, b):\n    return 0\n"}}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    logs = run_jobs()
    assert store.get_job(job.id).status == "failed"
    assert any("main checkout changed" in line for line in logs)
    git(git_repo, "checkout", "--", "src/calc.py")  # tidy up what the "rogue" CLI did


def test_cli_failure_fails_the_job_with_its_message(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 100}], error="Unexpected server error", exit=1)
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    logs = run_jobs()
    assert store.get_job(job.id).status == "failed"
    assert any("Unexpected server error" in line for line in logs)


# --- docs job ----------------------------------------------------------------------------

def test_docs_job_keeps_docstring_only_changes_and_reverts_the_rest(git_repo, store, scenario, run_jobs):
    (git_repo / "src" / "other.py").write_text('def double(x):\n    """Twice x."""\n    return x * 2\n')
    (git_repo / "README.md").write_text("# Sample\n\nIntro line.\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "more files")
    head = git(git_repo, "rev-parse", "HEAD")
    scenario(steps=[{"tokens": 11000, "writes": {
        "src/calc.py": 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n',
        "src/other.py": 'def double(x):\n    """Return x times two."""\n    return x * 2\n',  # rewrites a docstring
        "README.md": "# Sample\n\nIntro line.\n\n## calc\n\nAdds numbers.\n",
        "docs/calc.md": "# calc\n",
    }}])
    job = store.add_job("docs", "src", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    branch = f"gtu/{job.id}"
    assert '"""Return the sum of a and b."""' in git(git_repo, "show", f"{branch}:src/calc.py")
    assert git(git_repo, "show", f"{branch}:src/other.py") == 'def double(x):\n    """Twice x."""\n    return x * 2'
    assert "## calc" in git(git_repo, "show", f"{branch}:README.md")
    assert "docs/calc.md" in branch_files(git_repo, job.id)
    assert "reverted src/other.py" in store.steps(job.id)[1]["output"]
    assert_main_untouched(git_repo, head)
    # what the user gets: the kept files only, in spare-gpu/<date>_<time>/, with their paths
    folder = store.get_job(job.id).output_dir
    assert folder and folder.startswith("spare-gpu/")
    copied = sorted(p.relative_to(git_repo / folder).as_posix() for p in (git_repo / folder).rglob("*") if p.is_file())
    assert copied == ["README.md", "docs/calc.md", "src/calc.py"]  # other.py was reverted, so it isn't there
    assert '"""Return the sum of a and b."""' in (git_repo / folder / "src" / "calc.py").read_text()


def test_docs_job_never_gets_a_shell(git_repo, store, scenario, run_jobs):
    record = scenario(steps=[{"tokens": 100}])
    store.add_job("docs", "src", str(git_repo))
    run_jobs()
    assert json.loads(record.read_text().splitlines()[0])["config"]["permission"]["bash"] == "deny"


@pytest.mark.parametrize("old, new, ok", [
    ("def f():\n    return 1\n", 'def f():\n    """Doc."""\n    return 1\n', True),
    ('def f():\n    """Doc."""\n    return 1\n', 'def f():\n    """Doc. More detail."""\n    return 1\n', True),
    ('def f():\n    """Doc."""\n    return 1\n', "def f():\n    return 1\n", False),  # docstring deleted
    ("def f():\n    return 1\n", 'def f():\n    """Doc."""\n    return 2\n', False),  # code changed
    ("def f():\n    return 1\n", "def f(:\n", False),  # broken
])
def test_docs_only_change(old, new, ok):
    assert jobs.docs_only_change(old, new)[0] is ok


def test_markdown_may_only_grow():
    assert jobs.no_lines_removed("a\nb\n", "a\nb\nc\n")
    assert not jobs.no_lines_removed("a\nb\n", "a\nc\n")


# --- review job --------------------------------------------------------------------------

def test_review_job_writes_a_report_and_changes_no_source(git_repo, store, scenario, run_jobs, fake):
    head = git(git_repo, "rev-parse", "HEAD")
    fake.queue(Reply(content="## Likely bugs\n- `src/calc.py:2` no type checks\n\n## Risky code\nNothing found.\n\n## Suggestions\nNothing found.",
                     queue_wait_ms=4200))
    record = scenario()
    job = store.add_job("review", "src", str(git_repo))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    assert not record.exists()  # the review uses our own client, never the CLI
    name = f"gtu-review-{job.id}.md"
    assert branch_files(git_repo, job.id) == {"src/calc.py", name}
    report = git(git_repo, "show", f"gtu/{job.id}:{name}")
    assert report.startswith("# Code review: `src`") and "`src/calc.py:2`" in report
    sent = json.loads([r for r in fake.requests if r.url.path.endswith("/chat/completions")][0].content)
    assert "src/calc.py:1| def add(a, b):" in sent["messages"][1]["content"]
    (row,) = store.requests(job.id)
    assert row["queue_wait_ms"] == 4200 and row["request_id"]
    assert_main_untouched(git_repo, head)


def test_review_job_keeps_a_cut_off_report_and_says_so(git_repo, store, scenario, run_jobs, fake):
    fake.truncated("## Likely bugs\n- `src/calc.py:1` something")
    job = store.add_job("review", "src/calc.py", str(git_repo))
    run_jobs()
    report = git(git_repo, "show", f"gtu/{job.id}:gtu-review-{job.id}.md")
    assert "something" in report and "cut off at the output token limit" in report


# --- budgets -----------------------------------------------------------------------------

def test_job_budget_kills_the_cli_and_pauses_the_job(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 2000, "sleep": 0.3, "writes": {"tests/test_gtu_x.py": "def test_x():\n    pass\n"}}
                    for _ in range(6)])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    logs = run_jobs(budgets=Budgets(per_job=5000))
    assert store.get_job(job.id).status == "paused"
    assert [s["status"] for s in store.steps(job.id)] == ["pending", "pending"]
    used = store.request_summary(job.id)["total_tokens"]
    assert 5000 <= used <= 7000
    assert git(git_repo, "log", "--format=%s", f"main..gtu/{job.id}") == ""  # nothing half-done committed
    assert any("budget" in line for line in logs)


def night_budgets(**kw):
    """Budgets whose night began six hours ago, so usage logged "earlier tonight" stays in tonight
    however long the suite runs (a run that crossed the default 12:00 IST start saw it as last night)."""
    from datetime import datetime, timedelta

    from gpu_token_utilization.budget import IST

    return Budgets(night_start=(datetime.now(IST) - timedelta(hours=6)).time().replace(second=0, microsecond=0), **kw)


def test_what_is_left_of_the_night_budget_caps_a_cli_run(git_repo, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 2000, "sleep": 0.3} for _ in range(6)])
    store.log_request(RequestLog(endpoint="bharatcode-cli", attempt=1, total_tokens=995_000))  # earlier tonight
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    run_jobs(budgets=night_budgets(per_night=1_000_000, per_job=200_000))
    assert store.get_job(job.id).status == "paused"
    assert 5000 <= store.request_summary(job.id)["total_tokens"] <= 7000  # stopped near the 5,000 left


def test_night_budget_pauses_every_job(git_repo, store, scenario, run_jobs):
    record = scenario(steps=[{"tokens": 100}])
    store.log_request(RequestLog(endpoint="bharatcode-cli", attempt=1, total_tokens=950_000))
    first = store.add_job("tests", "src/calc.py", str(git_repo))
    second = store.add_job("review", "src", str(git_repo))
    logs = run_jobs(budgets=night_budgets(per_night=900_000))
    assert not record.exists()
    assert store.get_job(first.id).status == "paused" and store.get_job(second.id).status == "paused"
    assert any("tonight's token budget is used up" in line for line in logs)


def test_budget_night_starts_at_noon_ist():
    from datetime import datetime, timezone

    b = Budgets()
    late = datetime(2026, 10, 29, 22, 0, tzinfo=timezone.utc)  # 03:30 IST on the 30th
    assert b.night_began(late) == datetime(2026, 10, 29, 6, 30, tzinfo=timezone.utc)  # 12:00 IST on the 29th
    afternoon = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)  # 14:30 IST on the 30th
    assert b.night_began(afternoon) == datetime(2026, 10, 30, 6, 30, tzinfo=timezone.utc)


def test_budget_settings_from_env(monkeypatch):
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "1234")
    monkeypatch.setenv("GTU_NIGHT_TOKEN_BUDGET", "5678")
    monkeypatch.setenv("GTU_NIGHT_START", "20:00")
    b = Budgets.from_env()
    assert (b.per_job, b.per_night, b.night_start.hour) == (1234, 5678, 20)


# --- config --------------------------------------------------------------------------------

def test_test_command_must_be_allowlisted(monkeypatch):
    monkeypatch.setenv("GTU_TEST_COMMAND", "bash -c 'rm -rf /'")
    with pytest.raises(ValueError, match="not in the allowlist"):
        JobConfig.from_env()


@pytest.mark.parametrize("command, ok", [
    (("python", "-m", "pytest"), True),
    ((sys.executable, "-m", "pytest"), True),
    (("pytest", "-x"), True),
    (("npm", "test"), True),
    (("npm", "install"), False),
    (("python", "evil.py"), False),
])
def test_command_allowlist(command, ok):
    assert jobs.command_allowed(command, jobs.DEFAULT_ALLOWED) is ok
