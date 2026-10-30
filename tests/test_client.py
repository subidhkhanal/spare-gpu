import json

import pytest

from gpu_token_utilization.client import AuthError, BharatCodeClient, BusyError, RetryPolicy
from gpu_token_utilization.fakeapi import FakeBharatCode
from gpu_token_utilization.store import Store

KEY = "bc_live_fake_key"
MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture
def fake():
    return FakeBharatCode(api_key=KEY)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".gtu" / "gtu.db") as s:
        yield s


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GTU_TIMEOUT", "GTU_MAX_RETRIES", "GTU_RETRY_BASE", "GTU_RETRY_MAX_DELAY", "GTU_RETRY_JITTER"):
        monkeypatch.delenv(name, raising=False)


def make_client(fake, store, key=KEY, timeout=None):
    return BharatCodeClient(
        api_key=key,
        timeout=timeout,
        store=store,
        transport=fake.transport(),
        sleep=fake.clock.sleep,
        clock=fake.clock,
        rng=lambda: 0.5,  # middle of the ±jitter range, so delays are exact
    )


def test_success_is_logged_with_request_id_and_tokens(fake, store):
    fake.ok("hi there", prompt_tokens=12, completion_tokens=8, reasoning_tokens=3)
    result = make_client(fake, store).chat(MESSAGES)
    assert result.content == "hi there" and not result.truncated and result.attempts == 1
    assert result.usage.total_tokens == 20 and result.usage.reasoning_tokens == 3
    (row,) = store.requests()
    assert row["request_id"] == "fake-req-1"
    assert row["status_code"] == 200 and row["total_tokens"] == 20 and row["reasoning_tokens"] == 3
    assert row["truncated"] == 0 and row["error"] is None


def test_the_whole_usage_block_is_kept_including_fields_we_dont_parse(fake, store):
    fake.ok("hi", prompt_tokens=100, completion_tokens=5, usage_extra={"prompt_tokens_details": {"cached_tokens": 64}})
    result = make_client(fake, store).chat(MESSAGES)
    assert result.usage.raw["prompt_tokens_details"] == {"cached_tokens": 64}
    (row,) = store.requests()
    assert json.loads(row["usage_json"])["prompt_tokens_details"]["cached_tokens"] == 64


def test_request_body_follows_api_rules(fake, store):
    make_client(fake, store).chat(MESSAGES, max_tokens=50)
    body = json.loads(fake.requests[-1].content)
    assert body["stream"] is False and body["store"] is False
    assert body["reasoning_effort"] == "none" and body["max_tokens"] == 50
    assert body["model"] == "qwen-3.8-27b" and body["messages"] == MESSAGES


def test_busy_then_ok_backs_off_with_growing_delays(fake, store):
    fake.busy(2).ok("done")
    result = make_client(fake, store).chat(MESSAGES)
    assert result.content == "done" and result.attempts == 3
    assert fake.clock.sleeps == [5.0, 10.0]
    assert [r["status_code"] for r in store.requests()] == [503, 503, 200]
    assert store.request_summary()["busy"] == 2


def test_429_is_treated_like_busy(fake, store):
    fake.busy(1, status=429).ok()
    assert make_client(fake, store).chat(MESSAGES).attempts == 2


def test_gives_up_after_capped_retries(fake, store):
    fake.busy(10)
    client = make_client(fake, store)
    client.retry = RetryPolicy(max_retries=4, base_delay=30, max_delay=60)
    with pytest.raises(BusyError):
        client.chat(MESSAGES)
    assert len(fake.requests) == 5
    assert fake.clock.sleeps == [30.0, 60.0, 60.0, 60.0]


def test_retry_after_is_honoured_but_capped(fake, store):
    fake.busy(1, retry_after=30).busy(1, retry_after=999).ok()
    make_client(fake, store).chat(MESSAGES)
    assert fake.clock.sleeps == [30.0, 300.0]


def test_truncated_reply_keeps_partial_output_and_is_not_retried(fake, store):
    fake.truncated("half an ans")
    result = make_client(fake, store).chat(MESSAGES)
    assert result.truncated and result.content == "half an ans" and result.finish_reason == "length"
    assert len(fake.requests) == 1
    assert store.requests()[0]["truncated"] == 1


def test_truncated_tool_call_is_never_returned_as_executable(fake, store):
    fake.truncated("", tool_call=True)
    result = make_client(fake, store).chat(MESSAGES)
    assert result.tool_calls == []
    assert result.dropped_tool_calls[0]["function"]["name"] == "write_file"


def test_complete_tool_call_is_returned(fake, store):
    from gpu_token_utilization.fakeapi import Reply

    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
    fake.queue(Reply(content="", finish_reason="tool_calls", tool_calls=[call]))
    result = make_client(fake, store).chat(MESSAGES)
    assert result.tool_calls == [call] and result.dropped_tool_calls == []


def test_slow_reply_times_out_and_is_retried(fake, store):
    fake.slow(200).ok("finally")
    result = make_client(fake, store, timeout=60).chat(MESSAGES)
    assert result.content == "finally" and result.attempts == 2
    assert [r["error"] for r in store.requests()] == ["timeout", None]


def test_slow_but_within_timeout_is_measured(fake, store):
    fake.slow(30)
    result = make_client(fake, store, timeout=60).chat(MESSAGES)
    assert result.elapsed_ms == pytest.approx(30_000)
    assert store.requests()[0]["elapsed_ms"] == pytest.approx(30_000)


def test_bad_key_fails_fast_without_retry(fake, store):
    with pytest.raises(AuthError):
        make_client(fake, store, key="bc_live_wrong").chat(MESSAGES)
    assert len(fake.requests) == 1 and fake.clock.sleeps == []


def test_max_tokens_is_capped(fake, store):
    with pytest.raises(ValueError):
        make_client(fake, store).chat(MESSAGES, max_tokens=32_001)


def test_requests_are_tagged_with_job_and_step(fake, store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    step = store.add_step(job.id, "write tests")
    make_client(fake, store).chat(MESSAGES, job_id=job.id, step_id=step)
    row = store.requests(job.id)[0]
    assert row["job_id"] == job.id and row["step_id"] == step


def test_key_never_leaks(fake, store):
    fake.busy(1).ok()
    client = make_client(fake, store)
    client.chat(MESSAGES)
    assert KEY not in repr(client)
    for row in store.requests():
        assert KEY not in json.dumps(dict(row))


def test_default_retry_ladder_is_patient():
    policy = RetryPolicy()
    waits = [policy.delay(n, rng=lambda: 0.5) for n in range(1, policy.max_retries + 1)]
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    assert policy.total_wait() == 915.0


@pytest.mark.parametrize("rng, factor", [(0.0, 0.8), (0.5, 1.0), (1.0, 1.2)])
def test_jitter_is_plus_or_minus_twenty_percent(rng, factor):
    assert RetryPolicy().delay(3, rng=lambda: rng) == pytest.approx(20.0 * factor)


def test_jitter_never_exceeds_the_ceiling():
    assert RetryPolicy().delay(8, rng=lambda: 1.0) == 300.0


def test_gives_up_only_after_the_full_ladder(fake, store):
    fake.busy(20)
    with pytest.raises(BusyError):
        make_client(fake, store).chat(MESSAGES)
    assert len(fake.requests) == 9  # first try + 8 retries
    assert sum(fake.clock.sleeps) == 915.0


def test_default_timeout_is_180_seconds(fake, store):
    client = make_client(fake, store)
    assert client.timeout == 180.0
    fake.slow(170, content="slow but served")
    assert client.chat(MESSAGES).content == "slow but served"


def test_retry_and_timeout_settings_from_env(fake, store, monkeypatch):
    monkeypatch.setenv("GTU_TIMEOUT", "600")
    monkeypatch.setenv("GTU_MAX_RETRIES", "3")
    monkeypatch.setenv("GTU_RETRY_BASE", "10")
    monkeypatch.setenv("GTU_RETRY_MAX_DELAY", "15")
    monkeypatch.setenv("GTU_RETRY_JITTER", "0")
    client = make_client(fake, store)
    assert client.timeout == 600.0
    assert client.retry == RetryPolicy(max_retries=3, base_delay=10.0, max_delay=15.0, jitter=0.0)


def test_negative_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("GTU_RETRY_BASE", "-1")
    with pytest.raises(ValueError):
        RetryPolicy.from_env()


def test_scheduler_headers_are_logged(fake, store):
    from gpu_token_utilization.fakeapi import Reply

    fake.queue(Reply(content="ok", queue_wait_ms=14583))
    result = make_client(fake, store).chat(MESSAGES)
    assert result.queue_wait_ms == 14583
    row = store.requests()[0]
    assert row["queue_wait_ms"] == 14583
    assert row["scheduler_admitted_at_ms"] - row["scheduler_requested_at_ms"] == 14583


def test_missing_scheduler_headers_are_left_empty(fake, store):
    result = make_client(fake, store).chat(MESSAGES)
    assert result.queue_wait_ms is None and store.requests()[0]["queue_wait_ms"] is None


def test_sent_at_is_the_send_time_not_the_reply_time(fake, store):
    from datetime import datetime

    before = datetime.now().astimezone()
    before = before.replace(microsecond=before.microsecond // 1000 * 1000)  # sent_at keeps milliseconds
    fake.slow(1)  # fake time only; the real clock barely moves
    make_client(fake, store).chat(MESSAGES)
    after = datetime.now().astimezone()
    sent = datetime.fromisoformat(store.requests()[0]["sent_at"])
    assert before <= sent <= after
    assert "." in store.requests()[0]["sent_at"]  # millisecond precision


def test_each_retry_records_its_own_send_time(fake, store):
    fake.busy(1).ok()
    make_client(fake, store).chat(MESSAGES)
    first, second = (r["sent_at"] for r in store.requests())
    assert first <= second
