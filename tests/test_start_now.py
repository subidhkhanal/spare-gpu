"""`gtu start --now`: ignore the capacity gate, but keep recording capacity and stay polite."""

from datetime import datetime, timezone

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.cli import app
from gpu_token_utilization.gate import USAGE_URL
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

runner = CliRunner()


# --- the gate in bypass mode ---------------------------------------------------------------

def test_bypass_opens_even_when_the_lane_is_full(fake, make_gate):
    fake.usage(active=4, queued=9)
    decision = make_gate().check_bypassed()
    assert decision.open
    r = decision.reading
    assert r.source == "bypass" and r.verdict == "busy" and (r.active, r.queued, r.slots) == (4, 9, 4)
    assert decision.gate_info == {"gate_source": "bypass", "queue_at_send": 9, "active_at_send": 4, "slots_at_send": 4}


def test_bypass_still_reads_live_stats_at_most_once_a_minute(fake, make_gate):
    gate = make_gate()
    gate.check_bypassed()
    fake.clock.advance(30)
    assert gate.check_bypassed().open
    assert fake.usage_calls == 1
    fake.clock.advance(31)
    gate.check_bypassed()
    assert fake.usage_calls == 2


def test_bypass_pauses_one_interval_after_a_busy_answer(fake, make_gate):
    gate = make_gate()
    gate.check_bypassed()
    gate.force_busy("503 from BharatCode")
    paused = gate.check_bypassed()
    assert not paused.open and paused.next_check == pytest.approx(60)
    assert "--now ignores capacity, not busy answers" in paused.describe()
    fake.clock.advance(60)
    assert gate.check_bypassed().open


def test_bypass_works_when_live_stats_are_down(fake, make_gate):
    fake.usage(status=500)
    decision = make_gate().check_bypassed()
    assert decision.open and decision.reading.verdict == "unknown"
    assert "HTTP 500" in decision.reading.reason


# --- the scheduler -------------------------------------------------------------------------

def one_chat_step(ctx):
    ctx.chat([{"role": "user", "content": "hi"}])
    return "ok"


def test_ignore_gate_runs_jobs_immediately_into_a_full_lane(fake, store, client, make_gate):
    job = store.add_job("review", "a.py", "/repo")
    fake.usage_busy(10, queued=6)
    plans = {k: [("one", one_chat_step), ("two", one_chat_step)] for k in ("tests", "docs", "review")}
    done = Scheduler(store, make_gate(), client, plans=plans, sleep=fake.clock.sleep,
                     isolate=False, ignore_gate=True).run()
    assert done == 2 and store.get_job(job.id).status == "done"
    assert fake.clock.sleeps == []  # never waited for capacity
    rows = store.requests(job.id)
    assert {r["gate_source"] for r in rows} == {"bypass"} and {r["queue_at_send"] for r in rows} == {6}


def test_normal_mode_still_waits_with_the_same_full_lane(fake, store, client, make_gate):
    store.add_job("review", "a.py", "/repo")
    fake.usage_busy(3, queued=6)  # then quiet
    plans = {k: [("one", one_chat_step)] for k in ("tests", "docs", "review")}
    Scheduler(store, make_gate(), client, plans=plans, sleep=fake.clock.sleep, isolate=False).run()
    assert len(fake.clock.sleeps) >= 5  # 3 busy + 2 more quiet readings before the gate opened


# --- the command ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def full_lane():
    lane = {"active_requests": 4, "queued_requests": 7, "slot_capacity": 4, "capacity_used_pct": 100, "available": True}
    return httpx.Response(200, json={"generated_at": datetime.now(timezone.utc).isoformat(),
                                     "models": [{"id": "qwen-3.8-27b", "api": lane}]})


@respx.mock
def test_start_now_explains_itself_and_runs_a_review_into_a_full_lane(repo):
    respx.get(USAGE_URL).mock(side_effect=lambda request: full_lane())
    respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(
        200, headers={"x-request-id": "req-now-1"},
        json={"choices": [{"message": {"role": "assistant", "content": "## Likely bugs\nNothing found."},
                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
    ))
    added = runner.invoke(app, ["add", "review", "src/calc.py"])
    assert added.exit_code == 0, added.output
    job_id = added.output.split()[2].rstrip(":")

    result = runner.invoke(app, ["start", "--now"])
    assert result.exit_code == 0, result.output
    assert "--now: running queued jobs immediately" in result.output
    assert "Normally `gtu start` sends work only when BharatCode has a free slot" in result.output
    assert "Paused" not in result.output
    assert f"gtu-review-{job_id}.md" in git(repo, "ls-tree", "--name-only", "-r", f"gtu/{job_id}")


def test_start_without_now_does_not_print_the_note(repo, monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY")
    result = runner.invoke(app, ["start"])
    assert "--now: running" not in result.output
