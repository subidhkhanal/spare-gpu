"""`gtu start --dry-run`: shows jobs, steps and rough costs; sends and changes nothing."""

import re
import sys

import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization.cli import app
from gpu_token_utilization.store import RequestLog, Store

from conftest import git

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    monkeypatch.setenv("GTU_BHARATCODE_CMD", f'["{sys.executable}", "-m", "gpu_token_utilization.fakecli"]'.replace("\\", "/"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n" * 20)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def add(*args):
    result = runner.invoke(app, ["add", *args])
    assert result.exit_code == 0, result.output
    return re.search(r"Queued job (\w+)", result.output).group(1)


def db_counts(repo):
    with Store(repo / ".gtu" / "gtu.db") as s:
        return {t: s.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("jobs", "job_steps", "requests", "gate_readings")}, [j.status for j in s.list_jobs()]


@respx.mock  # no routes: any HTTP request at all would fail the test
def test_dry_run_lists_jobs_steps_and_costs_and_changes_nothing(repo):
    tests_id = add("tests", "src/calc.py")
    review_id = add("review", "src/calc.py")
    before = db_counts(repo)

    result = runner.invoke(app, ["start", "--dry-run"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Dry run: nothing is sent" in out
    assert f"Job {tests_id}  tests  src/calc.py  (queued)" in out
    assert "1. write   BharatCode CLI writes new test files" in out
    assert "2. verify  run them here, keep only passing tests, commit" in out
    assert "0 tokens (runs locally, no requests)" in out
    assert re.search(r"~3[\d,]+-[\d,]+ tokens \(estimate: 3-7 CLI steps", out)
    assert f"Job {review_id}  review" in out and "estimate from the code size" in out
    assert "Total for jobs that would run:" in out and "Start: immediately; BharatCode's busy answers are retried politely." in out
    assert "PROBLEM" not in out

    assert db_counts(repo) == before  # no steps, requests or gate readings created
    assert git(repo, "branch", "--list", "gtu/*") == ""
    assert not (repo / ".gtu" / "worktrees").exists()


@respx.mock
def test_dry_run_needs_no_api_key_but_mentions_it(repo, monkeypatch):
    add("review", "src/calc.py")
    monkeypatch.delenv("BHARATCODE_API_KEY")
    result = runner.invoke(app, ["start", "--dry-run"])
    assert result.exit_code == 0 and "PROBLEM: no BharatCode API key found (run `gtu setkey`)" in result.output
    assert "a run outside BharatCode Desktop would stop at once" in result.output  # inside Desktop no key is needed


def test_dry_run_with_nothing_queued(repo):
    result = runner.invoke(app, ["start", "--dry-run"])
    assert result.exit_code == 0 and "No queued jobs" in result.output


def test_dry_run_with_wait_for_capacity_says_it_would_hold_jobs(repo):
    add("review", "src/calc.py")
    result = runner.invoke(app, ["start", "--dry-run", "--wait-for-capacity"])
    assert "Start: only once BharatCode's live stats show a free slot (--wait-for-capacity)." in result.output
    assert "--wait-for-capacity: jobs start only" not in result.output  # the real-run note isn't printed


def test_partly_done_job_shows_only_remaining_steps_and_its_usage(repo):
    job_id = add("tests", "src/calc.py")
    with Store(repo / ".gtu" / "gtu.db") as s:
        write = s.add_step(job_id, "write")
        s.add_step(job_id, "verify")
        s.set_step(write, "done", "wrote tests")
        s.log_request(RequestLog(endpoint="bharatcode-cli", attempt=1, total_tokens=42_000, job_id=job_id, step_id=write))
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "1. verify" in out and "write   BharatCode" not in out
    assert "job budget: 42,000 used, 158,000 left of 200,000" in out


def test_estimates_come_from_earlier_runs_when_there_are_some(repo):
    with Store(repo / ".gtu" / "gtu.db") as s:
        for tokens in (30_000, 50_000):
            old = s.add_job("tests", "x.py", str(repo))
            step = s.add_step(old.id, "write")
            s.set_step(step, "done", "")
            s.log_request(RequestLog(endpoint="bharatcode-cli", attempt=1, total_tokens=tokens, job_id=old.id, step_id=step))
            s.set_status(old.id, "done")
    add("tests", "src/calc.py")
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "~30,000-50,000 tokens (from 2 earlier run(s))" in out


def test_over_budget_job_is_shown_as_skipped(repo, monkeypatch):
    job_id = add("review", "src/calc.py")
    with Store(repo / ".gtu" / "gtu.db") as s:
        s.log_request(RequestLog(endpoint="/chat/completions", attempt=1, total_tokens=5_000, job_id=job_id))
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "4000")
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "skipped: its token budget is used up (5,000 used of 4,000 for this job)" in out
    assert "Total for jobs that would run: ~0-0 tokens" in out


def test_high_estimate_over_the_remaining_budget_is_flagged(repo, monkeypatch):
    add("tests", "src/calc.py")
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "40000")
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "may pause partway" in out


def test_problems_that_would_stop_a_job_are_flagged(repo, monkeypatch):
    add("tests", "src/calc.py")
    monkeypatch.delenv("GTU_BHARATCODE_CMD")
    monkeypatch.setattr("gpu_token_utilization.agent_cli.shutil.which", lambda name: None)
    out = runner.invoke(app, ["start", "--dry-run"]).output
    assert "PROBLEM: BharatCode's CLI isn't installed" in out


def test_a_fix_job_without_the_cli_is_a_problem_in_the_dry_run(store, git_repo, monkeypatch):
    from gpu_token_utilization import agent_cli, dryrun

    monkeypatch.delenv("GTU_BHARATCODE_CMD", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    job = store.add_job("fix", ".", str(git_repo), note="src/calc.py:2 add a docstring")
    assert any("bharatcode" in p for p in dryrun._problems(job))  # like tests and docs: it runs the CLI
    assert dryrun._problems(store.add_job("review", "src", str(git_repo))) == []


def test_a_test_command_outside_the_allowlist_is_an_error_line_not_a_traceback(git_repo, monkeypatch):
    from typer.testing import CliRunner

    from gpu_token_utilization.cli import app

    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("GTU_TEST_COMMAND", "pypy -m pytest")
    result = CliRunner().invoke(app, ["start"])
    assert result.exit_code == 1 and "error: test command 'pypy -m pytest' is not in the allowlist" in result.output
    assert "Traceback" not in result.output and not isinstance(result.exception, ValueError)
