"""Without BharatCode Desktop: clear messages, and tests/docs jobs wait instead of failing."""

import httpx
import respx
from typer.testing import CliRunner

from gpu_token_utilization import desktop
from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

runner = CliRunner()


def test_without_desktop_tests_and_docs_jobs_wait_and_review_still_runs(fake, store, client, git_repo):
    tests_job = store.add_job("tests", "src", str(git_repo))
    docs_job = store.add_job("docs", "src", str(git_repo))
    review_job = store.add_job("review", "src", str(git_repo))
    fake.queue(Reply(content="## Likely bugs\nNothing found."))
    logs = []
    Scheduler(store, client, sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append,
              desktop_missing=desktop.DESKTOP_NEEDED).run()
    assert store.get_job(review_job.id).status == "done"
    assert store.get_job(tests_job.id).status == "queued" and store.get_job(docs_job.id).status == "queued"
    assert not store.steps(tests_job.id)  # nothing started, no worktree or branch made for it
    assert git(git_repo, "branch", "--list", f"gtu/{tests_job.id}") == ""
    text = "\n".join(logs)
    assert f"Job {tests_job.id} (tests): skipped, it needs BharatCode Desktop" in text and "stays queued" in text
    assert "2 job(s) are waiting for BharatCode Desktop" in text and "open this project in Desktop" in text


def test_a_job_step_with_no_desktop_fails_clearly_rather_than_crashing(store, git_repo):
    from gpu_token_utilization import jobs

    class Context:
        agent = None

    try:
        jobs.run_agent(Context(), "x", edit=True, commands=())
    except desktop.DesktopNotFound as exc:
        assert "BharatCode Desktop" in str(exc)
    else:
        raise AssertionError("expected DesktopNotFound")


def test_check_reports_desktop_and_does_not_fail_on_it(monkeypatch, tmp_path):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    monkeypatch.setenv("APPDATA", str(tmp_path / "nothing-installed"))
    with respx.mock:
        respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json={"data": []}))
        respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(200, json={}))
        result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "BharatCode Desktop: no config folder found yet" in result.output


def test_check_names_the_desktop_builds_it_finds(monkeypatch, tmp_path):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for name in ("BharatCode", "BharatCode Beta"):
        (tmp_path / name / "Config").mkdir(parents=True)
    result = runner.invoke(app, ["check"])
    assert "BharatCode Desktop: found (stable, beta)" in result.output
