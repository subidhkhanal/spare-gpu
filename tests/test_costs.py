"""The overhead/task split and the overhead multiplier (`spare_token_cost`, `gtu cost`)."""

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import costs
from gpu_token_utilization.cli import app
from gpu_token_utilization.store import RequestLog

FLOOR = costs.CLI_FLOOR_TOKENS


def cli_step(store, job, prompt, output):
    store.log_request(RequestLog(endpoint="bharatcode-cli", attempt=1, status_code=None, prompt_tokens=prompt,
                                 completion_tokens=output, total_tokens=prompt + output, job_id=job.id))


def api_request(store, job, prompt, output):
    store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, prompt_tokens=prompt,
                                 completion_tokens=output, total_tokens=prompt + output, job_id=job.id))


def test_a_cli_step_counts_the_measured_floor_as_overhead(store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    cli_step(store, job, prompt=FLOOR + 3_000, output=500)  # floor + 3,000 of code and conversation
    cost, _ = costs.collect(store, job.id)
    assert cost.cli.overhead == FLOOR and cost.cli.task == 3_500
    assert cost.multiplier == pytest.approx((FLOOR + 3_500) / 3_500)


def test_every_step_pays_the_floor_again(store, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    for _ in range(4):
        cli_step(store, job, prompt=FLOOR + 1_000, output=250)
    cost, _ = costs.collect(store, job.id)
    assert cost.overhead == 4 * FLOOR and cost.task == 4 * 1_250
    assert cost.multiplier == pytest.approx((4 * (FLOOR + 1_250)) / (4 * 1_250))  # 10.2x


def test_a_step_smaller_than_the_floor_is_all_overhead_and_never_negative(store, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    cli_step(store, job, prompt=2_000, output=0)
    cost, _ = costs.collect(store, job.id)
    assert cost.overhead == 2_000 and cost.task == 0 and cost.multiplier is None


def test_direct_requests_have_no_harness_overhead(store, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    api_request(store, job, prompt=3_000, output=700)
    cost, _ = costs.collect(store, job.id)
    assert cost.overhead == 0 and cost.multiplier == 1.0


def test_jobs_and_totals_mix_both_kinds(store, job_repo):
    tests = store.add_job("tests", "a.py", job_repo)
    review = store.add_job("review", "b.py", job_repo)
    cli_step(store, tests, prompt=FLOOR + 500, output=500)
    api_request(store, review, prompt=1_000, output=1_000)
    cost, _ = costs.collect(store, "all")
    assert cost.total == FLOOR + 3_000 and cost.overhead == FLOOR and cost.task == 3_000
    assert cost.jobs[tests.id].multiplier == pytest.approx((FLOOR + 1_000) / 1_000)
    assert cost.jobs[review.id].multiplier == 1.0


def test_the_floor_can_be_set_from_your_own_measurement(store, job_repo, monkeypatch):
    monkeypatch.setenv("GTU_CLI_OVERHEAD_TOKENS", "14407")
    job = store.add_job("tests", "a.py", job_repo)
    cli_step(store, job, prompt=20_000, output=0)
    assert costs.collect(store, job.id)[0].overhead == 14_407


def test_gtu_cost_prints_the_multiplier(tmp_path, monkeypatch):
    from gpu_token_utilization.store import Store

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    with Store.open_default() as store:
        job = store.add_job("tests", "a.py", str(tmp_path))
        cli_step(store, job, prompt=FLOOR + 1_000, output=500)
    result = CliRunner().invoke(app, ["cost", "all"])
    assert result.exit_code == 0, result.output
    assert f"Overhead {FLOOR:,}, task 1,500" in result.output and "overhead multiplier 8.7x" in result.output
