"""spare_capacity_advice / gtu advice: built on measured queue waits, honest when data is thin."""

from datetime import datetime, timedelta, timezone

from gpu_token_utilization import advice
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.store import RequestLog

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)  # 14:30 IST


def log(store, minutes_ago, wait_s, status=200, at=NOW):
    store.log_request(RequestLog(
        endpoint="/chat/completions", attempt=1, status_code=status,
        sent_at=(at - timedelta(minutes=minutes_ago)).isoformat(),
        queue_wait_ms=None if wait_s is None else int(wait_s * 1000),
    ))


def test_waits_come_from_bharatcodes_header_on_requests_through_the_fake_server(fake, store, client):
    fake.queue(Reply(queue_wait_ms=4000), Reply(queue_wait_ms=6000), Reply(queue_wait_ms=5000))
    for _ in range(3):
        client.chat([{"role": "user", "content": "hi"}], max_tokens=5)
    waits = advice.measured_waits(store)
    assert [w.seconds for w in waits] == [4.0, 6.0, 5.0]
    text = advice.advise(waits, now=datetime.now(timezone.utc))
    assert text.startswith("Fine to run now: median 5.0 s over our last 3 requests, the newest under a minute ago.")


def test_cold_start_with_no_data_at_all_says_we_do_not_know(store):
    text = advice.advise(advice.measured_waits(store), now=NOW)
    assert text.startswith("Not enough recent data to say how it is right now: none of our requests")
    assert "No measured waits at all yet, so we don't know what's usual" in text
    assert "Fine to run" not in text and "elevated" not in text


def test_cold_start_falls_back_to_this_hour_of_day(store):
    yesterday = NOW - timedelta(days=1)
    for minutes, wait in ((20, 8), (10, 12), (5, 40)):
        log(store, minutes, wait, at=yesterday)
    text = advice.advise(advice.measured_waits(store), now=NOW)
    assert text.startswith("Not enough recent data")
    assert "Around 14:00 IST our requests have waited a median 12.0 s (worst 40.0 s), from 3 requests on 1 day(s)." in text


def test_too_few_recent_requests_is_not_enough(store):
    log(store, 2, 1)
    log(store, 50, 1)  # too old to count as now
    text = advice.advise(advice.measured_waits(store), now=NOW)
    assert text.startswith("Not enough recent data to say how it is right now: only 1 of our requests")
    assert "Too little history for 14:00 IST" in text and "Across all 2 measured, the median wait was 1.0 s" in text


def test_elevated_waits(store):
    for minutes, wait in ((12, 45), (8, 60), (4, 38)):
        log(store, minutes, wait)
    text = advice.advise(advice.measured_waits(store), now=NOW)
    assert text.startswith("Waits are elevated right now: median 45.0 s over our last 3 requests, the newest 4 min ago.")


def test_trend_rising_falling_steady_or_unknown(store):
    for minutes, wait in ((18, 5), (15, 6), (6, 30), (3, 40)):
        log(store, minutes, wait)
    waits = advice.measured_waits(store)
    assert advice.trend(waits, NOW) == "rising"
    assert "waits look rising" in advice.advise(waits, now=NOW)
    falling = [advice.Wait(w.sent_at, s) for w, s in zip(waits, (40, 30, 6, 5))]
    assert advice.trend(falling, NOW) == "falling"
    steady = [advice.Wait(w.sent_at, s) for w, s in zip(waits, (10, 14, 12, 9))]
    assert advice.trend(steady, NOW) == "steady"
    assert advice.trend(waits[-3:], NOW) is None  # too few to call


def test_failed_requests_and_ones_without_the_header_are_ignored(store):
    log(store, 3, 99, status=503)
    log(store, 2, None)
    assert advice.measured_waits(store) == []
