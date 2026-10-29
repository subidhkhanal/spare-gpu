from datetime import datetime, time, timezone

import pytest

from gpu_token_utilization.gate import in_window, parse_window
from gpu_token_utilization.store import RequestLog


def checks(gate, fake, n):
    """Run n checks a poll interval apart; return the open/closed decisions."""
    out = []
    for _ in range(n):
        out.append(gate.check().open)
        fake.clock.advance(60)
    return out


def test_opens_only_after_three_quiet_readings(fake, make_gate):
    fake.usage_quiet(3)
    assert checks(make_gate(), fake, 3) == [False, False, True]


def test_polls_at_most_once_per_interval(fake, make_gate):
    gate = make_gate()
    gate.check()
    fake.clock.advance(30)
    decision = gate.check()
    assert fake.usage_calls == 1
    assert decision.next_check == pytest.approx(30)


def test_closes_after_two_busy_readings(fake, make_gate):
    fake.usage_quiet(3).usage_busy(2)
    assert checks(make_gate(), fake, 5) == [False, False, True, True, False]


def test_one_busy_blip_does_not_close(fake, make_gate):
    fake.usage_quiet(3).usage_busy(1).usage_quiet(1)
    assert checks(make_gate(), fake, 5) == [False, False, True, True, True]


def test_busy_reading_resets_the_quiet_streak(fake, make_gate):
    fake.usage_quiet(2).usage_busy(1).usage_quiet(3)
    assert checks(make_gate(), fake, 6) == [False, False, False, False, False, True]


@pytest.mark.parametrize(
    "fields",
    [
        {"active": 4, "queued": 0},  # no free slot
        {"active": 0, "queued": 6},  # queue beyond the default tolerance of 5
        {"active": 0, "queued": 0, "available": False},
    ],
)
def test_busy_conditions(fake, make_gate, fields):
    fake.usage(**fields)
    reading = make_gate().check().reading
    assert reading.source == "livestats" and reading.verdict == "busy"


@pytest.mark.parametrize("queued", [0, 3, 5])
def test_queue_within_tolerance_is_quiet_by_default(fake, make_gate, queued):
    # Account pacing leaves requests queued while slots sit idle (seen on the real API).
    fake.usage(active=0, queued=queued)
    assert make_gate().check().reading.verdict == "quiet"


def test_free_slot_is_still_required(fake, make_gate):
    fake.usage(active=4, queued=0, slots=4)
    assert make_gate(max_queued=None).check().reading.verdict == "busy"


def test_strict_mode_treats_any_queue_as_busy(fake, make_gate):
    fake.usage(active=0, queued=1)
    reading = make_gate(max_queued=0).check().reading
    assert reading.verdict == "busy" and "(tolerance 0)" in reading.reason


def test_queue_can_be_ignored_entirely(fake, make_gate):
    fake.usage(active=1, queued=40)
    assert make_gate(max_queued=None).check().reading.verdict == "quiet"


@pytest.mark.parametrize("value, expected", [("", 5), ("0", 0), ("12", 12), ("none", None), ("OFF", None)])
def test_max_queued_from_env(monkeypatch, value, expected):
    from gpu_token_utilization.gate import GateConfig

    monkeypatch.setenv("GTU_MAX_QUEUED", value)
    assert GateConfig.from_env().max_queued == expected


def test_force_busy_closes_immediately(fake, make_gate):
    fake.usage_quiet(3)
    gate = make_gate()
    assert checks(gate, fake, 3)[-1] is True
    decision = gate.force_busy("503 from BharatCode")
    assert not decision.open and decision.reading.source == "own"
    assert gate.check().open is False  # same minute: no new poll, stays closed
    fake.clock.advance(60)
    fake.usage_quiet(3)
    assert checks(gate, fake, 3) == [False, False, True]


@pytest.mark.parametrize(
    "fields, why",
    [
        ({"body": "<html>oops</html>"}, "unexpected shape"),
        ({"body": '{"generated_at": "2026-10-30T00:00:00Z", "models": [{"id": "qwen-3.8-27b", "api": '
                  '{"active_requests": "4", "queued_requests": 0, "slot_capacity": 4, "available": true}}]}'},
         "unexpected shape"),
        ({"drop_model": True}, "no api lane"),
        ({"status": 500}, "HTTP 500"),
        ({"delay": 30}, "unreachable"),
        ({"stale_seconds": 600}, "out of date"),
    ],
)
def test_bad_live_stats_fall_back(fake, make_gate, fields, why):
    fake.usage(**fields)
    reading = make_gate(probe=False).check().reading
    assert reading.source == "fallback" and why in reading.reason
    assert reading.verdict == "unknown"  # no request history yet, so no opinion


def test_fallback_judges_by_recent_requests(fake, store, make_gate):
    fake.usage(status=500, times=2)
    for _ in range(5):
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, elapsed_ms=900))
    gate = make_gate(probe=False)
    assert gate.check().reading.verdict == "quiet"
    for _ in range(2):
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=503, error="busy"))
    fake.clock.advance(60)
    reading = gate.check().reading
    assert reading.verdict == "busy" and "2 of the last 5" in reading.reason


def test_fallback_notices_slow_responses(fake, store, make_gate):
    fake.usage(status=500)
    for _ in range(20):
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, elapsed_ms=1000))
    for _ in range(5):
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, elapsed_ms=9000))
    reading = make_gate(probe=False, baseline_sample=25).check().reading
    assert reading.verdict == "busy" and "slow" in reading.reason


def test_fallback_probes_when_idle_but_not_too_often(fake, store, make_gate):
    fake.usage(status=500, times=10)
    gate = make_gate()
    gate.check()
    assert fake.chat_calls == 1
    assert store.requests()[0]["gate_source"] == "probe"
    fake.clock.advance(60)
    gate.check()
    assert fake.chat_calls == 1  # within the 5-minute probe interval
    fake.clock.advance(600)
    gate.check()
    assert fake.chat_calls == 2


def test_fallback_respects_the_window(fake, make_gate):
    fake.usage(status=500)
    now = fake.clock.utcnow()
    closed = (time(0, 0), time(0, 1)) if not in_window(now, (time(0, 0), time(0, 1))) else (time(12, 0), time(12, 1))
    reading = make_gate(window=closed).check().reading
    assert reading.verdict == "busy" and "outside the fallback window" in reading.reason


def test_window_is_ignored_for_live_stats_unless_asked(fake, make_gate):
    now = fake.clock.utcnow()
    closed = (time(0, 0), time(0, 1)) if not in_window(now, (time(0, 0), time(0, 1))) else (time(12, 0), time(12, 1))
    fake.usage_quiet(2)
    assert make_gate(window=closed).check().reading.verdict == "quiet"
    fake.clock.advance(60)
    assert make_gate(window=closed, window_always=True).check().reading.verdict == "busy"


def test_returns_to_live_stats_when_they_recover(fake, make_gate):
    fake.usage(status=500).usage_quiet(1)
    gate = make_gate(probe=False)
    assert gate.check().reading.source == "fallback"
    fake.clock.advance(60)
    assert gate.check().reading.source == "livestats"


def test_describe_explains_a_pause(fake, make_gate):
    fake.usage(active=4, queued=2)
    text = make_gate().check().describe()
    assert text.startswith("Paused (live stats): qwen-3.8-27b api lane 4/4 slots busy, 2 queued.")
    assert "Need 3 quiet readings in a row; have 0. Next check in 60 s." in text


def test_window_helpers():
    assert parse_window("22:00-06:00") == (time(22, 0), time(6, 0))
    late = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
    assert in_window(late, (time(22, 0), time(6, 0)))
    assert not in_window(late, (time(0, 0), time(9, 0)))
