"""Token totals for `gtu report` (per-session detail is in BharatCode Desktop itself)."""

from datetime import datetime, timedelta, timezone

import pytest

from gpu_token_utilization import costs
from gpu_token_utilization.store import RequestLog, StoreError


def step(store, job, prompt, output, sent_at=None):
    store.log_request(RequestLog(endpoint="bharatcode-desktop", attempt=1, status_code=None, prompt_tokens=prompt,
                                 completion_tokens=output, total_tokens=prompt + output, job_id=job.id,
                                 sent_at=sent_at))


def test_totals_per_job_and_overall(store, job_repo):
    tests = store.add_job("tests", "a.py", job_repo)
    review = store.add_job("review", "b.py", job_repo)
    step(store, tests, 8_000, 500)
    step(store, tests, 8_500, 300)
    step(store, review, 3_000, 700)
    cost, label = costs.collect(store, "all")
    assert label == "all time" and cost.total == 21_000
    assert cost.jobs[tests.id].total == 17_300 and cost.jobs[tests.id].requests == 2
    assert cost.jobs[review.id].completion == 700


def test_scope_by_hours_and_by_job(store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    now = datetime.now(timezone.utc)
    step(store, job, 1_000, 0, sent_at=(now - timedelta(hours=30)).isoformat())
    step(store, job, 2_000, 0, sent_at=(now - timedelta(hours=1)).isoformat())
    assert costs.collect(store, None, now)[0].total == 2_000  # the last 24 hours by default
    assert costs.collect(store, "48", now)[0].total == 3_000
    cost, label = costs.collect(store, job.id, now)
    assert cost.total == 3_000 and label.startswith(f"job {job.id}")


def test_an_all_digit_job_id_is_the_job_not_a_number_of_hours(store, job_repo, monkeypatch):
    # seen in CI on 5 Nov: job 16170455 was read as "the last 1.61705e+07 hours"
    monkeypatch.setattr("gpu_token_utilization.store.secrets.token_hex", lambda n: "16170455")
    job = store.add_job("tests", "a.py", job_repo)
    step(store, job, 1_000, 0)
    cost, label = costs.collect(store, "16170455")
    assert cost.total == 1_000 and label.startswith("job 16170455")
    assert costs.collect(store, "48")[1] == "the last 48 hours"  # a number that isn't a job is still hours


def test_an_unknown_job_id_is_an_error(store):
    with pytest.raises(StoreError):
        costs.collect(store, "nope1234")
