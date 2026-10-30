"""`gtu report`: summary of what ran, its cost, its output and the capacity it met."""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import report as reporting
from gpu_token_utilization import worktree
from gpu_token_utilization.agent_cli import BharatCodeCli
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

runner = CliRunner()
FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]
IMPORT_SRC = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\nfrom src.calc import add\n\n"
TESTS = IMPORT_SRC + (
    "def test_adds():\n    assert add(2, 3) == 5\n\n\n"
    "def test_negatives():\n    assert add(-1, -1) == -2\n\n\n"
    "def test_wrong():\n    assert add(1, 1) == 3\n"
)


@pytest.fixture
def ran(git_repo, store, client, make_gate, fake, tmp_path, monkeypatch):
    """A tests job and a review job, run to completion against the fakes."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [{"tokens": 11500, "writes": {"tests/test_gtu_calc.py": TESTS}},
                                              {"tokens": 500}]}))
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(scenario))
    fake.usage_default.active = 1  # one of four slots busy: sends count as "into a free slot"
    fake.queue(Reply(content="## Likely bugs\nNothing found.", queue_wait_ms=4200,
                     prompt_tokens=300, completion_tokens=40))
    tests_job = store.add_job("tests", "src/calc.py", str(git_repo))
    review_job = store.add_job("review", "src/calc.py", str(git_repo))
    Scheduler(store, make_gate(), client, cli=BharatCodeCli(command=FAKE_CLI, timeout=60), budgets=Budgets(),
              config=JobConfig(test_command=(sys.executable, "-m", "pytest")), sleep=fake.clock.sleep,
              now=fake.clock.utcnow).run()
    return store.get_job(tests_job.id), store.get_job(review_job.id)


def test_job_report_counts_what_ran(ran, store):
    tests_job, review_job = ran
    t = reporting.build_job(store, tests_job)
    assert t.job.status == "done" and t.started and t.finished and t.duration >= timedelta(0)
    assert t.tokens == 12000 and t.cli_steps == 2 and t.api_requests == 0
    assert (t.tests_kept, t.tests_dropped) == (2, 1)
    assert t.files == [("A", "tests/test_gtu_calc.py")]
    assert t.queue_waits_ms == []  # the CLI doesn't expose it
    assert t.sends_with_stats == 2 and t.free_slot_share == 1.0

    r = reporting.build_job(store, review_job)
    assert r.api_requests == 1 and r.tokens == 340 and r.queue_waits_ms == [4200]
    assert r.files == [("A", f"gtu-review-{review_job.id}.md")]
    assert r.queue_depths == [0]


def test_report_is_committed_to_the_branch_without_touching_the_checkout(ran, store, git_repo):
    tests_job, _ = ran
    (git_repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # my uncommitted edit\n")
    head, status = git(git_repo, "rev-parse", "HEAD"), git(git_repo, "status", "--porcelain")

    r = reporting.build_job(store, tests_job)
    commit = reporting.write_to_branch(r)
    branch = f"gtu/{tests_job.id}"
    assert commit and git(git_repo, "rev-parse", branch) == commit
    text = git(git_repo, "show", f"{branch}:gtu-report-{tests_job.id}.md")
    assert "kept 2 passing, discarded 1 failing" in text and "`tests/test_gtu_calc.py` (added)" in text
    assert "Sent into a free slot: 2 of 2 (100%)" in text
    assert git(git_repo, "log", "-1", "--format=%an", branch) == "spare-gpu"
    # the user's checkout: same commit, same uncommitted edit, no report file
    assert git(git_repo, "rev-parse", "HEAD") == head and git(git_repo, "status", "--porcelain") == status
    assert "my uncommitted edit" in (git_repo / "src" / "calc.py").read_text()
    assert not (git_repo / f"gtu-report-{tests_job.id}.md").exists()

    assert reporting.write_to_branch(reporting.build_job(store, tests_job)) == "unchanged"
    assert git(git_repo, "rev-parse", branch) == commit  # no duplicate commit


def test_report_file_is_not_listed_as_job_output(ran, store):
    tests_job, _ = ran
    reporting.write_to_branch(reporting.build_job(store, tests_job))
    assert reporting.build_job(store, tests_job).files == [("A", "tests/test_gtu_calc.py")]


def test_unfinished_job_gets_no_branch_report(git_repo, store):
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    worktree.create(git_repo, job.id)
    store.set_status(job.id, "paused")
    assert reporting.write_to_branch(reporting.build_job(store, store.get_job(job.id))) is None


def test_summary_covers_jobs_tokens_and_capacity(ran, store):
    reports = [reporting.build_job(store, j) for j in ran]
    written = {r.job.id: reporting.write_to_branch(r) for r in reports}
    now = datetime.now(timezone.utc)
    text = reporting.summary_markdown(reports, store, Budgets(), now, now - timedelta(hours=1), written)
    assert "| Job | Kind | Target | Status | Ran | Took | Tokens | Produced |" in text
    assert "2 tests kept, 1 discarded" in text
    assert "**Tokens:** 12,340 across these jobs" in text
    assert "BharatCode's reported queue wait: median 4.2 s, max 4.2 s over 1 request(s)" in text
    assert "Sent into a free slot: 3 of 3 (100%)" in text
    assert f"Report committed to `gtu/{ran[0].id}`" in text


def test_job_markdown_is_deterministic(ran, store):
    r = reporting.build_job(store, ran[0])
    assert reporting.job_markdown(r) == reporting.job_markdown(reporting.build_job(store, ran[0]))


def test_select_jobs_by_period_and_id(ran, store):
    tests_job, review_job = ran
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert reporting.select_jobs(store, future, None) == []
    assert {j.id for j in reporting.select_jobs(store, None, None)} == {tests_job.id, review_job.id}
    assert [j.id for j in reporting.select_jobs(store, future, review_job.id)] == [review_job.id]


def test_report_command_prints_and_saves(ran, store, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("GTU_DB", str(store.path))
    result = runner.invoke(app, ["report", "--since", "24"])
    assert result.exit_code == 0, result.output
    assert "spare-gpu morning report" in result.output and ran[0].id in result.output
    saved = list((store.path.parent / "reports").glob("report-*.md"))
    assert len(saved) == 1 and "## Capacity context" in saved[0].read_text()
    assert f"gtu-report-{ran[0].id}.md" in git(git_repo, "ls-tree", "--name-only", "-r", f"gtu/{ran[0].id}")


def test_report_with_nothing_in_the_period(git_repo, monkeypatch, tmp_path):
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("GTU_DB", str(tmp_path / "empty" / "gtu.db"))
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0 and "Nothing ran in this period." in result.output


def test_add_file_to_branch_refuses_a_missing_branch(git_repo):
    with pytest.raises(worktree.GitError):
        worktree.add_file_to_branch(git_repo, "gtu/nope", "x.md", "x", "msg")
