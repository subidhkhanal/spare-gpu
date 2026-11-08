"""The green ("make the tests pass") and implement (a Markdown plan file) job kinds, end to end: a
real git repo whose tests fail at first, the fake BharatCode Desktop scripted per session, the real
scheduler and real pytest. Plus the places that list job kinds: the dry run, spare_delegate, the
store's CHECK constraint, the settings and the README."""

import json
import sqlite3
import sys
from pathlib import Path

import anyio
import httpx
import mcp
import pytest
from typer.testing import CliRunner

from gpu_token_utilization import actions, settings
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.mcp_server import build_server
from gpu_token_utilization.router import Choice
from gpu_token_utilization.scheduler import Scheduler, kept_nothing
from gpu_token_utilization.store import JOB_KINDS, SCHEMA, Store

from conftest import git, quick_plans, quick_tools

ROOT = Path(__file__).resolve().parents[1]

# Generated and shipped test files put the repo root on sys.path, as a real tests folder would via conftest.
IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
TEST_ADD = IMPORT_SRC + "def test_adds_numbers():\n    assert add(2, 3) == 5\n"
WRONG = "def add(a, b):\n    return a - b\n"  # what the project ships: its test fails
STILL_WRONG = "def add(a, b):\n    return a * b\n"  # a first attempt: 2 * 3 is not 5 either
RIGHT = "def add(a, b):\n    return a + b\n"
PLAN = "# Plan\n\n1. Add `subtract(a, b)` to `src/calc.py`.\n2. Add a test for it in `tests/test_gtu_subtract.py`.\n"
# The project's own test command, allowed whatever the interpreter is called (python3 on Linux).
CONFIG = JobConfig(test_command=(sys.executable, "-m", "pytest"),
                   allowed_commands=("python -m pytest", "python3 -m pytest", "pytest"))
DEEPSEEK, QWEN = "deepseek-v4.1-flash", "qwen-3.8-27b"


def config(**changes) -> JobConfig:
    return JobConfig(test_command=CONFIG.test_command, allowed_commands=CONFIG.allowed_commands, **changes)


@pytest.fixture
def run_jobs(store, client, fake, desktop_fake):
    def run(config=CONFIG, **kwargs):
        logs = []
        Scheduler(
            store, client, agent=desktop_fake.agent(timeout=60), budgets=Budgets(), config=config,
            sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append, **kwargs,
        ).run()
        return logs

    return run


@pytest.fixture
def red_repo(git_repo):
    """A committed repo whose one test fails: add subtracts."""
    (git_repo / "src" / "calc.py").write_text(WRONG)
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_calc.py").write_text(TEST_ADD)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "red")
    return git_repo


@pytest.fixture
def repo_with_plan(git_repo):
    """A committed repo whose test passes, with a plan file to implement."""
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_calc.py").write_text(TEST_ADD)
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "plan.md").write_text(PLAN)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "green, with a plan")
    return git_repo


def calls(desktop):
    """Each Desktop session the job started, in order: its prompt, model and permission file."""
    return [{"prompt": s["prompt"]["parts"][0]["text"],
             "model": f"bharatcode/{s['prompt']['model']['modelID']}", "config": s["config"]}
            for s in desktop.sessions.values() if s["prompt"]]


def result_files(store, job_id):
    """The files a finished job left in its spare-gpu/<date>_<time>/ folder (empty if none)."""
    job = store.get_job(job_id)
    if not job.output_dir:
        return set()
    root = Path(job.repo) / job.output_dir
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def result(store, job_id, path):
    job = store.get_job(job_id)
    return (Path(job.repo) / job.output_dir / path).read_text(encoding="utf-8")


def assert_main_untouched(repo, head):
    """Same commit, same branch, nothing changed, and no branch or copy of the job left behind."""
    assert git(repo, "rev-parse", "main") == head
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    worktrees = Path(repo) / ".gtu" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


def assert_write_permissions(call):
    """A write step gets edit rights and the test command, nothing more, denied first."""
    permission = call["config"]["permission"]
    assert permission["*"] == "deny" and permission["edit"] == "allow"
    bash = permission["bash"]
    assert list(bash)[0] == "*" and bash["*"] == "deny" and bash["python -m pytest*"] == "allow"
    assert permission["webfetch"] == "deny"


# --- green ---------------------------------------------------------------------------------

def test_green_fixes_the_code_on_the_second_round_and_exports_it(red_repo, store, scenario, run_jobs):
    head = git(red_repo, "rev-parse", "HEAD")
    desktop = scenario(calls=[
        [{"tokens": 3000, "writes": {"src/calc.py": STILL_WRONG}}],  # round 1: wrong again
        [{"tokens": 3000, "writes": {"src/calc.py": RIGHT}}],  # round 2: right
    ])
    job = store.add_job("green", ".", str(red_repo))
    logs = run_jobs()

    assert store.get_job(job.id).status == "done"
    steps = store.steps(job.id)
    assert [(s["seq"], s["name"], s["status"]) for s in steps] == \
        [(1, "write", "done"), (2, "verify", "done"), (3, "write", "done"), (4, "verify", "done")]
    first_verify, last_verify = steps[1]["output"], steps[3]["output"]
    assert first_verify.startswith("Round 1 of 3: tests still fail; trying again.")
    assert "Test output (exit code 1):" in first_verify and "test_adds_numbers" in first_verify
    assert last_verify.startswith("Tests pass after 2 round(s). Changed 1 file(s): src/calc.py.")
    assert not kept_nothing(first_verify) and not kept_nothing(last_verify)
    assert result_files(store, job.id) == {"src/calc.py"} and result(store, job.id, "src/calc.py") == RIGHT
    assert any("step 3 of 4 (write) started" in line for line in logs)

    first, second = calls(desktop)
    prompt = first["prompt"]
    assert prompt.startswith("Make the tests pass in this repository.") and "fix the code, not the tests" in prompt
    assert "Do not modify or delete any existing test file" in prompt and "The code to fix is in this repository." in prompt
    assert "test_adds_numbers" in prompt and "assert -1 == 5" in prompt  # the failing output went to the model
    assert "assert 6 == 5" in second["prompt"]  # round 2 started from round 1's attempt, and saw its failure
    assert_write_permissions(first)
    assert_main_untouched(red_repo, head)


def test_green_gives_up_after_its_rounds_and_keeps_nothing(red_repo, store, scenario, run_jobs):
    head = git(red_repo, "rev-parse", "HEAD")
    desktop = scenario(calls=[
        [{"tokens": 3000, "writes": {"src/calc.py": STILL_WRONG, "src/extra.py": "x = 1\n"}}],
        [{"tokens": 3000, "writes": {"src/calc.py": "def add(a, b):\n    return a - b - 1\n", "src/more.py": "y = 2\n"}}],
    ])
    job = store.add_job("green", "src", str(red_repo))
    run_jobs(config=config(green_rounds=2))

    assert store.get_job(job.id).status == "done"
    steps = store.steps(job.id)
    assert [(s["name"], s["status"]) for s in steps] == [("write", "done"), ("verify", "done")] * 2
    assert steps[1]["output"].startswith("Round 1 of 2: tests still fail; trying again.")
    last = steps[3]["output"]
    assert last.startswith("Tests still fail after 2 round(s); nothing kept.") and kept_nothing(last)
    assert "- discarded every change: the test command still fails (exit code 1)" in last
    assert "Test output:" in last and "test_adds_numbers" in last
    # nothing exported: not round 1's files (saved in the copy) nor round 2's
    assert result_files(store, job.id) == set() and store.get_job(job.id).output_dir is None
    assert not (red_repo / "spare-gpu").exists()
    assert len(calls(desktop)) == 2 and "The code to fix is in `src`." in calls(desktop)[0]["prompt"]
    assert_main_untouched(red_repo, head)


def test_green_with_tests_already_passing_starts_no_session_and_says_so(repo_with_plan, store, scenario, run_jobs):
    desktop = scenario(steps=[{"tokens": 3000, "writes": {"src/calc.py": STILL_WRONG}}])  # would break it, if called
    job = store.add_job("green", ".", str(repo_with_plan))
    run_jobs()
    assert store.get_job(job.id).status == "done"
    steps = store.steps(job.id)
    assert [s["name"] for s in steps] == ["write", "verify"]
    assert steps[0]["output"] == "Tests already pass; nothing to do."
    assert steps[1]["output"] == "Tests already pass; nothing to do." and not kept_nothing(steps[1]["output"])
    assert not calls(desktop)  # no Desktop session was started
    assert store.get_job(job.id).output_dir is None and not (repo_with_plan / "spare-gpu").exists()


def test_green_never_passes_by_changing_an_existing_test(red_repo, store, scenario, run_jobs):
    weakened = IMPORT_SRC + "def test_adds_numbers():\n    assert add(2, 3) == -1\n"
    scenario(steps=[{"tokens": 3000, "writes": {"tests/test_calc.py": weakened, "tests/test_gtu_new.py": IMPORT_SRC + "def test_new():\n    assert add(1, 1) == 0\n"}}])
    job = store.add_job("green", ".", str(red_repo))
    run_jobs(config=config(green_rounds=1))
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Tests still fail after 1 round(s); nothing kept.")
    assert "- reverted tests/test_calc.py: a green job may not change existing tests" in verify
    assert "reverted tests/test_gtu_new.py" not in verify  # a new test file is allowed (it went with the rest when the round failed)
    assert result_files(store, job.id) == set()


def test_a_routed_green_job_that_gives_up_reruns_from_round_one_on_the_preferred_model(red_repo, store, scenario, run_jobs):
    desktop = scenario(calls=[
        [{"tokens": 3000, "writes": {"src/calc.py": STILL_WRONG}}],  # on Qwen: wrong
        [{"tokens": 3000, "writes": {"src/calc.py": RIGHT}}],  # the fallback rerun on DeepSeek: right
    ])
    job = store.add_job("green", ".", str(red_repo))
    run_jobs(config=config(green_rounds=1), route=lambda j: Choice(QWEN, DEEPSEEK, "faster"))
    job = store.get_job(job.id)
    assert job.status == "done" and job.model == DEEPSEEK and job.fallback == 1
    steps = store.steps(job.id)
    assert [(s["name"], s["status"]) for s in steps] == [("write", "done"), ("verify", "done")]  # the same two steps, rerun
    assert steps[1]["output"].startswith("Tests pass after 1 round(s). Changed 1 file(s): src/calc.py.")
    assert result(store, job.id, "src/calc.py") == RIGHT
    models = [c["model"] for c in calls(desktop)]
    assert models == [f"bharatcode/{QWEN}", f"bharatcode/{DEEPSEEK}"]


# --- implement -----------------------------------------------------------------------------

def test_implement_keeps_the_plans_result_when_the_tests_pass(repo_with_plan, store, scenario, run_jobs):
    head = git(repo_with_plan, "rev-parse", "HEAD")
    desktop = scenario(steps=[{"tokens": 4000, "writes": {
        "src/calc.py": RIGHT + "\n\ndef subtract(a, b):\n    return a - b\n",
        "tests/test_gtu_subtract.py": IMPORT_SRC.replace("import add", "import subtract") + "def test_subtracts():\n    assert subtract(5, 3) == 2\n",
        "tests/test_calc.py": "def test_nothing():\n    pass\n",  # an existing test weakened
    }}])
    job = store.add_job("implement", "docs/plan.md", str(repo_with_plan))
    run_jobs()

    assert store.get_job(job.id).status == "done"
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Implemented the plan; tests pass. Kept 2 file(s).")
    assert "- kept src/calc.py" in verify and "- kept tests/test_gtu_subtract.py" in verify
    assert "- reverted tests/test_calc.py: an implement job may not change existing tests" in verify
    assert not kept_nothing(verify)
    assert result_files(store, job.id) == {"src/calc.py", "tests/test_gtu_subtract.py"}
    assert "def subtract" in result(store, job.id, "src/calc.py")
    (call,) = calls(desktop)
    prompt = call["prompt"]
    assert prompt.startswith("Implement the plan below in this repository. The plan is the file `docs/plan.md`.")
    assert PLAN.strip() in prompt and "Do not modify or delete existing test files" in prompt
    assert_write_permissions(call)
    assert_main_untouched(repo_with_plan, head)


def test_implement_drops_a_result_that_fails_the_tests_or_changes_nothing(repo_with_plan, store, scenario, run_jobs):
    scenario(steps=[{"tokens": 4000, "writes": {"src/calc.py": WRONG + "\n\ndef subtract(a, b):\n    return a - b\n"}}])
    job = store.add_job("implement", "docs/plan.md", str(repo_with_plan))
    run_jobs()
    verify = store.steps(job.id)[1]["output"]
    assert verify.startswith("Tests fail after implementing the plan; nothing kept.") and kept_nothing(verify)
    assert "- discarded the changes: the test command failed with them (exit code 1)" in verify
    assert result_files(store, job.id) == set() and store.get_job(job.id).output_dir is None

    scenario(steps=[{"tokens": 500}], text="The plan is already done.")
    nothing = store.add_job("implement", "docs/plan.md", str(repo_with_plan))
    run_jobs()
    verify = store.steps(nothing.id)[1]["output"]
    assert verify == "Nothing was implemented; nothing kept." and kept_nothing(verify)
    assert store.get_job(nothing.id).status == "done"


def test_implement_needs_a_markdown_plan_file_in_the_project(repo_with_plan, store, scenario, run_jobs, monkeypatch):
    open_store = lambda repo: Store(repo / ".gtu" / "gtu.db")  # noqa: E731
    with pytest.raises(actions.ActionError, match="src/calc.py is not a Markdown file; an implement job needs a plan file ending in .md"):
        actions.queue_job(open_store, repo_with_plan, "implement", "src/calc.py")
    with pytest.raises(actions.ActionError, match="docs is not a file; an implement job needs a Markdown plan file"):
        actions.queue_job(open_store, repo_with_plan, "implement", "docs")
    with pytest.raises(actions.ActionError, match="does not exist"):
        actions.queue_job(open_store, repo_with_plan, "implement", "docs/missing.md")
    assert actions.queue_job(open_store, repo_with_plan, "implement", "docs/plan.md").job.target == "docs/plan.md"
    # the same through `gtu add`
    monkeypatch.chdir(repo_with_plan)
    monkeypatch.delenv("GTU_DB", raising=False)
    outcome = CliRunner().invoke(app, ["add", "implement", "src/calc.py"])
    assert outcome.exit_code == 1 and "src/calc.py is not a Markdown file" in outcome.output
    # a job queued another way is refused by the step itself, and the job fails with the reason
    scenario(steps=[{"tokens": 500}])
    job = store.add_job("implement", "src/calc.py", str(repo_with_plan))
    run_jobs()
    assert store.get_job(job.id).status == "failed"
    assert "src/calc.py is not a Markdown file" in store.steps(job.id)[0]["output"]


# --- the places that list job kinds ---------------------------------------------------------

def test_the_dry_run_lists_green_and_implement_jobs(repo_with_plan, monkeypatch):
    monkeypatch.chdir(repo_with_plan)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    runner = CliRunner()
    for args in (["add", "green", "."], ["add", "implement", "docs/plan.md"]):
        added = runner.invoke(app, args)
        assert added.exit_code == 0, added.output
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "  green  .  (queued)" in out and "  implement  docs/plan.md  (queued)" in out
    assert "1. write   run the test command here; if it fails, a Desktop session fixes the code (never the tests)" in out
    assert "2. verify  run the test command here; done if it passes, else another round (up to green_rounds)" in out
    assert "1. write   a Desktop session carries out the Markdown plan file" in out
    assert "2. verify  run the test command here, keep the changes only if it passes, commit" in out
    assert "estimate: 4-10 session steps" in out and "PROBLEM" not in out


def test_spare_delegate_accepts_green_and_implement(repo_with_plan, fake):
    base = quick_plans()
    plans = {**base, "green": base["tests"], "implement": base["tests"]}
    tools = quick_tools(repo_with_plan, lambda: httpx.Client(transport=fake.transport()), plans=plans)
    assert tools.delegate("green", ".").startswith("Done: green for . (job ")
    assert tools.delegate("implement", "docs/plan.md").startswith("Done: implement for docs/plan.md (job ")
    refused = tools.delegate("implement", "src/calc.py")
    assert refused.startswith("Didn't hand it over: src/calc.py is not a Markdown file")

    async def go():
        async with mcp.Client(build_server(repo_with_plan, tools)) as client:
            return (await client.list_tools()).tools

    delegate = {t.name: t for t in anyio.run(go)}["spare_delegate"]
    assert {"green", "implement"} <= set(delegate.input_schema["properties"]["type"]["enum"])
    description = " ".join(delegate.description.split())
    assert "make the tests pass" in description and "implement docs/plan.md" in description


def test_an_older_store_learns_green_and_implement_and_keeps_its_rows(tmp_path, job_repo):
    # a store made before the two kinds existed: its CHECK constraint names only the first four
    path = tmp_path / ".gtu" / "gtu.db"
    path.parent.mkdir()
    kinds_sql = ", ".join(f"'{k}'" for k in JOB_KINDS)
    assert kinds_sql in SCHEMA
    old = sqlite3.connect(path)
    old.executescript(SCHEMA.replace(kinds_sql, "'tests', 'docs', 'review', 'fix'"))
    old.execute("INSERT INTO jobs (id, kind, target, repo, note, created_at, updated_at) VALUES "
                "('old00001', 'fix', '.', ?, 'keep me', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')", (job_repo,))
    old.execute("INSERT INTO job_steps (job_id, seq, name, status) VALUES ('old00001', 1, 'write', 'done')")
    old.commit()
    with pytest.raises(sqlite3.IntegrityError):
        old.execute("INSERT INTO jobs (id, kind, target, repo, created_at, updated_at) VALUES ('x', 'green', '.', 'r', 'a', 'a')")
    old.close()
    with Store(path) as store:
        green = store.add_job("green", ".", job_repo)
        plan = store.add_job("implement", "docs/plan.md", job_repo)
        assert (green.kind, plan.kind) == ("green", "implement") and store.get_job("old00001").note == "keep me"
        assert [s["name"] for s in store.steps("old00001")] == ["write"]
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = store.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
        assert "'green'" in sql and "'implement'" in sql
    with Store(path) as again:  # a second open changes nothing
        assert [j.id for j in again.list_jobs()] == ["old00001", green.id, plan.id]


def test_green_rounds_setting_is_read_validated_and_written_by_init(git_repo, monkeypatch):
    monkeypatch.delenv("GTU_GREEN_ROUNDS", raising=False)
    assert JobConfig.load(git_repo).green_rounds == 3 and settings.KEYS["green_rounds"][0] == "GTU_GREEN_ROUNDS"
    folder = git_repo / ".gtu"
    folder.mkdir()
    (folder / "settings.json").write_text(json.dumps({"green_rounds": 5}), encoding="utf-8")
    assert JobConfig.load(git_repo).green_rounds == 5
    monkeypatch.setenv("GTU_GREEN_ROUNDS", "2")  # the environment wins
    assert JobConfig.load(git_repo).green_rounds == 2
    for bad in ("0", "11", "two", "2.5"):
        monkeypatch.setenv("GTU_GREEN_ROUNDS", bad)
        with pytest.raises(ValueError, match="whole number from 1 to 10"):
            JobConfig.load(git_repo)
    monkeypatch.delenv("GTU_GREEN_ROUNDS")
    for bad in (0, 11, True, "3", 2.5):
        (folder / "settings.json").write_text(json.dumps({"green_rounds": bad}), encoding="utf-8")
        with pytest.raises(ValueError, match="green_rounds must be a whole number of rounds from 1 to 10"):
            JobConfig.load(git_repo)
    (folder / "settings.json").unlink()
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    assert json.loads((folder / "settings.json").read_text(encoding="utf-8"))["green_rounds"] == 3


def test_the_readme_documents_every_setting_and_the_two_kinds():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key, (env, _) in settings.KEYS.items():
        assert f"| `{key}` | `{env}` |" in readme, key
    assert "`/auto make the tests pass`" in readme and "/auto implement docs/plan.md" in readme
    assert '"fix the failing tests"' in readme and "`green` job" in readme and "`implement` job" in readme
    auto = (ROOT / "src" / "gpu_token_utilization" / "commands" / "auto.md").read_text(encoding="utf-8")
    assert '"green" ("make the tests pass", "fix the failing tests"' in auto and '"implement" ("implement docs/plan.md"' in auto
