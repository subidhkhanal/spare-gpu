"""Progress output: waiting for the gate or a retry must never look like a hang."""


import httpx
import respx
from typer.testing import CliRunner

from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.client import BharatCodeClient, RetryPolicy
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import KEY, git

runner = CliRunner()


def test_every_retry_wait_is_announced(fake, store):
    messages = []
    client = BharatCodeClient(api_key=KEY, store=store, transport=fake.transport(), sleep=fake.clock.sleep,
                              clock=fake.clock, rng=lambda: 0.5, on_retry=messages.append, retry=RetryPolicy())
    fake.busy(6).ok()
    client.chat([{"role": "user", "content": "hi"}])
    assert messages == [
        f"BharatCode is busy (HTTP 503). Waiting {w}s before retry {n} of 8." for n, w in
        enumerate([5, 10, 20, 40, 80, 160], start=1)
    ]


def test_timeouts_are_announced_too(fake, store):
    messages = []
    client = BharatCodeClient(api_key=KEY, store=store, transport=fake.transport(), sleep=fake.clock.sleep,
                              clock=fake.clock, rng=lambda: 0.5, timeout=60, on_retry=messages.append)
    fake.slow(200).ok()
    client.chat([{"role": "user", "content": "hi"}])
    assert messages == ["BharatCode timed out. Waiting 5s before retry 1 of 8."]


def test_scheduler_narrates_each_step(fake, store, client, job_repo):
    store.add_job("review", "a.py", job_repo)
    fake.queue(Reply(content="report", queue_wait_ms=6200))
    logs = []

    def review(ctx):
        ctx.chat([{"role": "user", "content": "review"}])
        return "done"

    Scheduler(store, client, plans={"review": [("review", review)]}, sleep=fake.clock.sleep,
              isolate=False, log=logs.append).run()
    assert logs[0] == "1 job(s) to run. Starting now."
    assert logs[1].endswith("step 1 of 1 (review) started: one request.")
    assert logs[2] == "  Sending request to BharatCode..."
    assert logs[3].startswith("  Answer received in ") and "6.2 s of it in BharatCode's queue" in logs[3]
    assert logs[-1] == "Queue empty."


def test_session_steps_are_narrated_as_they_finish(git_repo, store, client, fake, scenario):
    desktop = scenario(steps=[{"tokens": 11500}, {"tokens": 300}])
    store.add_job("docs", "src/calc.py", str(git_repo))
    logs = []
    Scheduler(store, client, agent=desktop.agent(timeout=60), budgets=Budgets(),
              config=JobConfig(), sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
    assert any("step 1 of 2 (write) started: BharatCode Desktop is working on it (Qwen 3.8 27B)" in line for line in logs)
    step_lines = [line for line in logs if "BharatCode Desktop finished a step" in line]
    assert len(step_lines) == 2 and "(11,500 tokens)" in step_lines[0] and "(300 tokens)" in step_lines[1]
    assert any("step 2 of 2 (verify) started: running locally." in line for line in logs)


@respx.mock
def test_start_shows_retry_waits_live(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    monkeypatch.setenv("GTU_RETRY_BASE", "0")  # real sleeps in this test, so make them instant
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    ok = httpx.Response(200, json={"choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
                                   "usage": {"total_tokens": 50}})
    respx.post(f"{BASE_URL}/chat/completions").mock(side_effect=[httpx.Response(503), httpx.Response(503), ok])
    assert runner.invoke(app, ["add", "review", "a.py"]).exit_code == 0
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0, result.output
    assert "BharatCode is busy (HTTP 503). Waiting 0s before retry 1 of 8." in result.output
    assert "Waiting 0s before retry 2 of 8." in result.output
    assert "Answer received in" in result.output
