"""BharatCode's CLI missing or signed out: clear messages, and tests/docs jobs wait instead of failing."""

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization import agent_cli
from gpu_token_utilization.agent_cli import cli_status
from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

runner = CliRunner()
MISSING = "BharatCode's CLI isn't installed"


@pytest.fixture
def no_cli(monkeypatch):
    monkeypatch.delenv("GTU_BHARATCODE_CMD", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)


def test_missing_cli_explains_node_npm_and_login(no_cli):
    status = cli_status()
    assert status.command is None and MISSING in status.problem
    assert "https://nodejs.org" in status.problem and "npm install -g bharatcode" in status.problem
    assert "bharatcode auth login" in status.problem and "review jobs don't" in status.problem


def test_status_of_an_installed_signed_in_cli():
    status = cli_status()
    assert status.problem is None and status.version == "0.0.0-fake" and status.signed_in is True


def test_status_of_a_signed_out_cli(monkeypatch):
    monkeypatch.setenv("GTU_FAKE_CLI_SIGNED_IN", "0")
    assert cli_status().signed_in is False


def test_a_cli_that_does_not_run_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("GTU_BHARATCODE_CMD", str(tmp_path / "no-such-program.exe"))
    status = cli_status()
    assert status.problem and "didn't run" in status.problem


# --- gtu check ----------------------------------------------------------------------------

def test_check_reports_a_missing_cli_but_does_not_fail_on_it(no_cli, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    with respx.mock:
        respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json={"data": []}))
        respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(200, json={}))
        result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "BharatCode CLI: not available" in result.output and "npm install -g bharatcode" in result.output


def test_check_reports_the_cli_even_without_a_key(monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "BharatCode CLI: installed (0.0.0-fake) and signed in" in result.output


def test_check_says_when_the_cli_is_signed_out(monkeypatch):
    monkeypatch.setenv("GTU_FAKE_CLI_SIGNED_IN", "0")
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["check"])
    assert "not signed in. Run `bharatcode auth login`" in result.output


# --- gtu add and gtu start ----------------------------------------------------------------

def test_add_tests_warns_when_the_cli_is_missing(git_repo, monkeypatch, no_cli):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    result = runner.invoke(app, ["add", "tests", "src"])
    assert result.exit_code == 0 and "Queued job" in result.output
    assert "won't run yet" in result.output and "npm install -g bharatcode" in result.output
    review = runner.invoke(app, ["add", "review", "src"])
    assert "won't run yet" not in review.output


def test_without_the_cli_tests_and_docs_jobs_wait_and_review_still_runs(fake, store, client, make_gate, git_repo):
    tests_job = store.add_job("tests", "src", str(git_repo))
    docs_job = store.add_job("docs", "src", str(git_repo))
    review_job = store.add_job("review", "src", str(git_repo))
    fake.queue(Reply(content="## Likely bugs\nNothing found."))
    logs = []
    Scheduler(store, make_gate(), client, sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append,
              ignore_gate=True, cli_missing=f"{MISSING}.\nnpm install -g bharatcode").run()
    assert store.get_job(review_job.id).status == "done"
    assert store.get_job(tests_job.id).status == "queued" and store.get_job(docs_job.id).status == "queued"
    assert not store.steps(tests_job.id)  # nothing started, no worktree or branch made for it
    assert git(git_repo, "branch", "--list", f"gtu/{tests_job.id}") == ""
    text = "\n".join(logs)
    assert f"Job {tests_job.id} (tests): skipped" in text and "stays queued" in text
    assert "2 job(s) are waiting for BharatCode's CLI" in text and "npm install -g bharatcode" in text
