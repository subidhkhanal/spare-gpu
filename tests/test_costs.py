"""Token totals per job and period (per-session detail is in BharatCode Desktop itself), and
/cost (the pack's cost_summary tool): BharatCode's per-model figures next to spare-gpu's jobs."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from gpu_token_utilization import costs
from gpu_token_utilization.store import RequestLog, StoreError


def step(store, job, prompt, output, sent_at=None):
    store.log_request(RequestLog(endpoint="bharatcode-desktop", attempt=1, status_code=None, prompt_tokens=prompt,
                                 completion_tokens=output, total_tokens=prompt + output, job_id=job.id,
                                 sent_at=sent_at))


def test_totals_per_job_and_overall(store, job_repo):
    first = store.add_job("fix", "a.py", job_repo)
    second = store.add_job("fix", "b.py", job_repo)
    step(store, first, 8_000, 500)
    step(store, first, 8_500, 300)
    step(store, second, 3_000, 700)
    cost, label = costs.collect(store, "all")
    assert label == "all time" and cost.total == 21_000
    assert cost.jobs[first.id].total == 17_300 and cost.jobs[first.id].requests == 2
    assert cost.jobs[second.id].completion == 700


def test_scope_by_hours_and_by_job(store, job_repo):
    job = store.add_job("fix", "a.py", job_repo)
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
    job = store.add_job("fix", "a.py", job_repo)
    step(store, job, 1_000, 0)
    cost, label = costs.collect(store, "16170455")
    assert cost.total == 1_000 and label.startswith("job 16170455")
    assert costs.collect(store, "48")[1] == "the last 48 hours"  # a number that isn't a job is still hours


def test_an_unknown_job_id_is_an_error(store):
    with pytest.raises(StoreError):
        costs.collect(store, "nope1234")


# --- /cost: BharatCode's per-model figures next to spare-gpu's jobs ----------------------------------

from gpu_token_utilization import stats  # noqa: E402
from gpu_token_utilization.mcp_server import SpareTools  # noqa: E402

# What Desktop's own record gives per model (stats.tokens_since), for a day's chats.
USAGE = {"bharatcode/deepseek-v4.1-flash": {"messages": 12, "input": 120_500, "output": 8_200, "reasoning": 0,
                                            "cache_read": 40_000, "cache_write": 0, "tokens": 168_700},
         "bharatcode/qwen-3.8-27b": {"messages": 30, "input": 300_000, "output": 20_000, "reasoning": 0,
                                     "cache_read": 0, "cache_write": 0, "tokens": 320_000}}


def finished(store, repo, kind, target, status, tokens, verify=None, when=None):
    """A job that ended with `status`, having used `tokens`, its verify step saying `verify`;
    `when` (ISO) backdates it."""
    job = store.add_job(kind, target, repo)
    if verify is not None:
        step_id = store.add_step(job.id, "verify")
        store.set_step(step_id, "done", verify)
    step(store, job, tokens, 0, sent_at=when)
    store.set_status(job.id, status)
    if when:
        with store.conn:
            store.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (when, job.id))
    return job


def test_period_combines_the_model_figures_with_the_jobs(store, job_repo):
    now = datetime.now(timezone.utc)
    a = finished(store, job_repo, "fix", "src/pay.py", "done", 20_000, verify="Kept the fix in 3 file(s): the test command passed.")
    b = finished(store, job_repo, "fix", "README.md", "done", 15_000, verify="Kept the fix in 0 file(s): the test command failed with it.")
    c = finished(store, job_repo, "fix", "src/r.py", "failed", 10_000)
    d = finished(store, job_repo, "fix", "src/f.py", "cancelled", 500)
    old = finished(store, job_repo, "fix", "old.py", "done", 99_000, verify="Kept the fix in 3 file(s): the test command passed.",
                   when=(now - timedelta(days=3)).isoformat(timespec="seconds"))
    period = costs.period(store, days=1, now=now, usage=USAGE)
    assert period.ran == 4 and period.count("done") == 2 and period.count("failed") == 1 and period.count("cancelled") == 1
    assert period.tokens == 45_500 and period.kept_nothing == 1
    assert [j.job.id for j in period.top] == [a.id, b.id, c.id]  # three most expensive; d and the old job aren't there
    assert {j.job.id for j in period.jobs} == {a.id, b.id, c.id, d.id}

    text = costs.render(period)
    assert text.startswith("Cost for the last 24 hours\n")
    assert "Tokens per model (exact, from BharatCode Desktop's own record" in text
    rows = re.findall(r"^  (Qwen 3\.8 27B|DeepSeek V4\.1 Flash)\s+(\d+)\s+([\d,]+)$", text, re.M)
    assert rows == [("Qwen 3.8 27B", "30", "320,000"),  # the larger first
                    ("DeepSeek V4.1 Flash", "12", "168,700")]
    assert "spare-gpu jobs: 4 job(s) ran (2 done, 1 failed, 1 cancelled), 45,500 tokens; 1 kept nothing after its check." in text
    assert f"{b.id}  done, kept nothing" in text and f"{c.id}  failed" in text
    assert f"1. fix src/pay.py ({a.id}): 20,000 tokens, done" in text
    assert f"2. fix README.md ({b.id}): 15,000 tokens, done, kept nothing" in text
    assert old.id not in text and "\u2014" not in text

    short = costs.render_short(period)
    assert "\n" not in short and short.startswith("In the last 24 hours: By model (")
    assert "Qwen 3.8 27B 320,000 tokens in 30 message(s); DeepSeek V4.1 Flash 168,700 tokens in 12 message(s)." in short
    assert "spare-gpu: 4 job(s) ran (2 done, 1 failed, 1 cancelled) for 45,500 tokens, and 1 kept nothing after its check." in short
    assert f"Most expensive: fix src/pay.py ({a.id}) 20,000 tokens, done; fix README.md ({b.id}) 15,000 tokens, done, kept nothing; " in short


def test_period_without_desktop_data_says_so_and_without_jobs_too(store):
    period = costs.period(store, days=1, usage=None)
    assert period.models is None and period.jobs == [] and period.top == []
    text = costs.render(period)
    no_data = "BharatCode Desktop's session data wasn't found on this machine, so no per-model figures"
    assert no_data in text and "spare-gpu jobs: none ran in the last 24 hours." in text
    short = costs.render_short(period)
    assert short == f"In the last 24 hours: {no_data}. spare-gpu ran no jobs in the last 24 hours."
    assert costs.render_short(costs.period(store, days=7, usage={}, channel="stable")).startswith(
        "In the last 7 days: BharatCode Desktop (stable) recorded no replies in this period; "
        "`/cost beta` (or stable, dev, local) reads another Desktop build.")


def test_period_reads_desktops_record_from_the_start_of_the_period(store, monkeypatch, tmp_path):
    asked = []

    def fake_tokens_since(since, directory=None, db=None):
        asked.append((since, directory))
        return USAGE

    monkeypatch.setattr(stats, "tokens_since", fake_tokens_since)
    monkeypatch.setattr(stats, "session_db", lambda env=None, which=None: tmp_path / "bharatcode.db")
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    assert costs.period(store, days=3, now=now).models["bharatcode/qwen-3.8-27b"]["tokens"] == 320_000
    assert costs.period(store, days=0.5, now=now).models is not None
    # exactly the period asked for, every project (no folder)
    assert asked == [(now - timedelta(days=3), None), (now - timedelta(hours=12), None)]


def test_a_running_job_with_requests_in_the_period_counts_as_ran(store, job_repo):
    job = store.add_job("fix", "a.py", job_repo)
    store.set_status(job.id, "running")
    step(store, job, 2_000, 0)
    period = costs.period(store, usage=None)
    assert [j.job.id for j in period.jobs] == [job.id] and period.count("done") == 0 and not period.jobs[0].kept_nothing
    assert "1 job(s) ran, 2,000 tokens" in costs.render(period)


def test_the_cost_summary_tool(store, job_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("GTU_DB", str(store.path))
    monkeypatch.setattr(stats, "tokens_since", lambda since, directory=None, db=None: USAGE)
    monkeypatch.setattr(stats, "session_db", lambda env=None, which=None: store.path)  # any file: the figures are faked
    finished(store, job_repo, "fix", "src/pay.py", "done", 20_000, verify="Kept the fix in 0 file(s): the test command failed with it.")
    tools = SpareTools(tmp_path)
    tables = tools.cost_summary(short=False)
    assert "Tokens per model" in tables and "1 job(s) ran (1 done), 20,000 tokens; 1 kept nothing" in tables
    short = tools.cost_summary(days=2)
    assert "\n" not in short and short.startswith("In the last 2 days: By model")
    monkeypatch.setattr(stats, "session_db", lambda env=None, which=None: None)
    assert "BharatCode Desktop's session data wasn't found" in tools.cost_summary(short=False)
    assert tools.cost_summary(days=0) == "Give a period of more than 0 days."


def test_the_channel_option_reads_that_desktops_record(store, job_repo, monkeypatch, tmp_path):
    from conftest import desktop_db

    monkeypatch.setenv("GTU_DB", str(store.path))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    reply = {"input": 900, "output": 100, "reasoning": 0, "cache": {"read": 0, "write": 0}, "total": 1000}
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    desktop_db(local / "BharatCode Beta" / "Data" / "bharatcode.db", [("C:/x", now_ms - 1000, "bharatcode/qwen-3.8-27b", reply)])
    tools = SpareTools(tmp_path)
    beta = tools.cost_summary(channel="beta")
    assert "Qwen 3.8 27B 1,000 tokens in 1 message(s)" in beta
    stable = tools.cost_summary()  # not started by Desktop: the released app's, which has no data here
    assert "session data wasn't found" in stable
    bad = tools.cost_summary(channel="nightly")
    assert "unknown Desktop build 'nightly'" in bad
