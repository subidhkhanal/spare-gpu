"""Wait signals per model (spare_waits) and the estimate the router ranks models by."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gpu_token_utilization import waits
from gpu_token_utilization.gate import LaneStats
from gpu_token_utilization.store import RequestLog

NOW = datetime(2026, 11, 4, 9, 45, tzinfo=timezone.utc)  # 15:15 IST, so "10 minutes ago" is the same hour


@pytest.fixture(autouse=True)
def fresh_cache():
    waits._cache.clear()
    yield
    waits._cache.clear()


def queue_waits_body(as_of=NOW, deepseek=(15800, 103000, 146000), qwen=(3, 3, 4)):
    return {"window_minutes": 60, "as_of": as_of.isoformat().replace("+00:00", "Z"), "models": [
        {"id": "deepseek-v4.1-flash", "sample_count": 331, "p50_ms": deepseek[0], "p75_ms": deepseek[1], "p90_ms": deepseek[2]},
        {"id": "qwen-3.8-27b", "sample_count": 275, "p50_ms": qwen[0], "p75_ms": qwen[1], "p90_ms": qwen[2]},
    ]}


def usage_body(deepseek=(2, 8, 2), qwen=(1, 0, 4)):
    def lane(active, queued, slots):
        return {"active_requests": active, "queued_requests": queued, "slot_capacity": slots, "available": True}
    return {"generated_at": NOW.isoformat().replace("+00:00", "Z"), "models": [
        {"id": "deepseek-v4.1-flash", "api": lane(*deepseek)}, {"id": "qwen-3.8-27b", "api": lane(*qwen)}]}


def client(queue=None, usage=None, calls=None):
    def handle(request):
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path.endswith("/queue-waits"):
            return queue if isinstance(queue, httpx.Response) else httpx.Response(200, json=queue or queue_waits_body())
        return usage if isinstance(usage, httpx.Response) else httpx.Response(200, json=usage or usage_body())
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_queue_waits_are_parsed_per_model():
    got = waits.queue_waits(client(), NOW)
    assert got["deepseek-v4.1-flash"].p75_ms == 103000 and got["qwen-3.8-27b"].p50_ms == 3


@pytest.mark.parametrize("reply", [
    httpx.Response(500),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"window_minutes": 60, "as_of": "2026-11-04T09:30:00Z"}),  # no models
    httpx.Response(200, json=queue_waits_body(as_of=NOW - timedelta(minutes=10))),  # stale
])
def test_a_broken_or_stale_answer_is_no_data_never_no_wait(reply):
    assert waits.queue_waits(client(queue=reply), NOW) is None


def test_each_endpoint_is_asked_at_most_once_a_minute():
    calls, t = [], [0.0]
    http = client(calls=calls)
    for at in (0, 30, 59):
        t[0] = at
        waits.queue_waits(http, NOW, clock=lambda: t[0])
    assert calls.count("/api/serving/queue-waits") == 1
    t[0] = 61
    waits.queue_waits(http, NOW, clock=lambda: t[0])
    assert calls.count("/api/serving/queue-waits") == 2


def test_own_waits_are_per_model_recent_and_by_hour(store):
    def log(model, minutes_ago, wait_s, status=200):
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=status, model=model,
                                     sent_at=(NOW - timedelta(minutes=minutes_ago)).isoformat(),
                                     queue_wait_ms=int(wait_s * 1000)))
    log("deepseek-v4.1-flash", 10, 90)
    log("bharatcode/deepseek-v4.1-flash", 5, 120)  # the same model under its full name
    log("qwen-3.8-27b", 5, 1)
    log("deepseek-v4.1-flash", 24 * 60, 60)  # yesterday, same hour: usual, not recent
    log("deepseek-v4.1-flash", 3, 999, status=503)  # failed requests don't count
    recent, usual = waits.own_waits(store, "deepseek-v4.1-flash", NOW)
    assert recent == [90.0, 120.0] and sorted(usual) == [60.0, 90.0, 120.0]


def lane(active, queued, slots=4, available=True):
    return LaneStats(active_requests=active, queued_requests=queued, slot_capacity=slots, available=available)


def test_a_free_slot_with_an_empty_queue_means_about_no_wait():
    g = waits.queue_waits(client(), NOW)["qwen-3.8-27b"]
    est = waits.estimate(waits.View("qwen-3.8-27b", g, lane(1, 0)))
    assert est.seconds == pytest.approx(0.003) and est.basis == "free slot, empty queue"


def test_a_busy_lane_blends_the_global_p75_with_your_own_waits():
    g = waits.queue_waits(client(), NOW)["deepseek-v4.1-flash"]
    est = waits.estimate(waits.View("deepseek-v4.1-flash", g, lane(2, 8, 2), recent=[50.0, 60.0, 70.0]))
    assert est.seconds == pytest.approx(0.6 * 103 + 0.4 * 60)
    assert "p75 last hour" in est.basis and "your last 3 waits" in est.basis


def test_without_live_figures_the_usual_for_the_hour_is_used_then_unknown():
    assert waits.estimate(waits.View("x", usual=[10.0, 20.0, 30.0])).seconds == 20.0
    assert waits.estimate(waits.View("x", usual=[10.0])).seconds is None
    assert waits.estimate(waits.View("x", lane=lane(0, 0, available=False))).seconds is None


def test_describe_names_the_faster_model_only_when_it_matters(store):
    text = waits.describe(waits.views(store, client(), NOW))
    assert "DeepSeek V4.1 Flash: everyone, last hour: p50 16 s, p75 1m 43s" in text
    assert "Qwen 3.8 27B would start about" in text and "spare_delegate" in text
    waits._cache.clear()
    even = client(queue=httpx.Response(200, json=queue_waits_body(deepseek=(3, 4, 5))), usage=httpx.Response(
        200, json=usage_body(deepseek=(0, 0, 2))))
    assert "No meaningful difference" in waits.describe(waits.views(store, even, NOW))


def test_describe_says_when_there_is_no_data(store):
    broken = client(queue=httpx.Response(500), usage=httpx.Response(500))
    text = waits.describe(waits.views(store, broken, NOW))
    assert "no data right now" in text and "Not enough data to compare" in text


def test_model_ids_ignore_the_provider_prefix():
    assert waits.model_id("bharatcode/qwen-3.8-27b") == waits.model_id("qwen-3.8-27b") == "qwen-3.8-27b"
