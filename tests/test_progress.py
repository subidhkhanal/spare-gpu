"""Progress output: waiting for the gate or a retry must never look like a hang."""

import json
import sys
from datetime import datetime, timezone

import httpx
import respx
from typer.testing import CliRunner

from gpu_token_utilization.agent_cli import BharatCodeCli
from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.client import BharatCodeClient, RetryPolicy
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.gate import USAGE_URL
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import KEY, git

runner = CliRunner()
FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]


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


def test_scheduler_narrates_the_gate_and_each_step(fake, store, client, make_gate, job_repo):
    store.add_job("review", "a.py", job_repo)
    fake.usage_busy(2)
    fake.queue(Reply(content="report", queue_wait_ms=6200))
    logs = []

    def review(ctx):
        ctx.chat([{"role": "user", "content": "review"}])
        return "done"

    Scheduler(store, make_gate(), client, plans={"review": [("review", review)]}, sleep=fake.clock.sleep,
              isolate=False, log=logs.append).run()
    assert logs[0] == "1 job(s) to run. Waiting for a free slot first (--wait-for-capacity)."
    closed = [line for line in logs if line.startswith("Gate closed")]
    assert len(closed) == 4  # 2 busy readings, then 2 quiet ones before the third opens it
    assert "Next check in 60s" in closed[0] and "0 so far" in closed[0]
    assert closed[-1].endswith("(opens after 3 quiet readings in a row; 2 so far).")
    i = logs.index("Gate open: qwen-3.8-27b api lane 0/4 slots busy, 0 queued.")
    assert logs[i + 1].endswith("step 1 of 1 (review) started: one request.")
    assert logs[i + 2] == "  Sending request to BharatCode..."
    assert logs[i + 3].startswith("  Answer received in ") and "6.2 s of it in BharatCode's queue" in logs[i + 3]
    assert logs[-1] == "Queue empty."


def test_cli_steps_are_narrated_as_they_finish(git_repo, store, client, make_gate, fake, tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [{"tokens": 11500}, {"tokens": 300}]}))
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(scenario))
    store.add_job("docs", "src/calc.py", str(git_repo))
    logs = []
    Scheduler(store, make_gate(), client, cli=BharatCodeCli(command=FAKE_CLI, timeout=60), budgets=Budgets(),
              config=JobConfig(), sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
    assert any("step 1 of 2 (write) started: the BharatCode CLI is working on it" in line for line in logs)
    cli_lines = [line for line in logs if "BharatCode CLI finished a step" in line]
    assert len(cli_lines) == 2 and "(11,500 tokens)" in cli_lines[0] and "(300 tokens)" in cli_lines[1]
    assert any("step 2 of 2 (verify) started: running locally." in line for line in logs)


def test_why_still_reads_well():
    from gpu_token_utilization.gate import Decision, GateConfig
    from gpu_token_utilization.store import GateReading

    reading = GateReading(taken_at="2026-10-30T00:00:00+00:00", source="livestats", verdict="busy", decision="closed",
                          quiet_streak=1, busy_streak=0, reason="qwen-3.8-27b api lane 4/4 slots busy, 3 queued")
    assert Decision(False, reading, 60, GateConfig()).describe() == (
        "Gate closed: qwen-3.8-27b api lane 4/4 slots busy, 3 queued. Next check in 60s "
        "(opens after 3 quiet readings in a row; 1 so far).")


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
    lane = {"active_requests": 1, "queued_requests": 0, "slot_capacity": 4, "capacity_used_pct": 25, "available": True}
    respx.get(USAGE_URL).mock(side_effect=lambda r: httpx.Response(
        200, json={"generated_at": datetime.now(timezone.utc).isoformat(), "models": [{"id": "qwen-3.8-27b", "api": lane}]}))
    ok = httpx.Response(200, json={"choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
                                   "usage": {"total_tokens": 50}})
    respx.post(f"{BASE_URL}/chat/completions").mock(side_effect=[httpx.Response(503), httpx.Response(503), ok])
    assert runner.invoke(app, ["add", "review", "a.py"]).exit_code == 0
    result = runner.invoke(app, ["start", "--now"])
    assert result.exit_code == 0, result.output
    assert "BharatCode is busy (HTTP 503). Waiting 0s before retry 1 of 8." in result.output
    assert "Waiting 0s before retry 2 of 8." in result.output
    assert "Answer received in" in result.output
