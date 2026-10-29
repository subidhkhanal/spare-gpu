import json

import pytest

from gpu_token_utilization.scheduler import DEFAULT_PLANS, Scheduler


def counting_plan(names, calls, crash_on=None):
    """Steps that each send one chat request and record that they ran."""

    def make(name):
        def step(ctx):
            calls.append((name, ctx.decision.reading.verdict))
            if crash_on and crash_on.get(name):
                crash_on[name] -= 1
                raise KeyboardInterrupt  # stands in for the process being killed mid-step
            ctx.chat([{"role": "user", "content": name}])
            return f"{name} output"

        return step

    return {kind: [(n, make(n)) for n in names] for kind in ("tests", "docs", "review")}


def make_scheduler(store, gate, client, fake, plans):
    return Scheduler(store, gate, client, plans=plans, sleep=fake.clock.sleep)


def test_pauses_while_busy_and_resumes_when_quiet(fake, store, client, make_gate):
    job = store.add_job("tests", "a.py", "/repo")
    fake.usage_busy(4)  # then the default reply: quiet
    calls = []
    done = make_scheduler(store, make_gate(), client, fake, counting_plan(["one", "two"], calls)).run()
    assert done == 2 and store.get_job(job.id).status == "done"
    verdicts = [r["verdict"] for r in store.conn.execute("SELECT verdict FROM gate_readings ORDER BY id")]
    assert verdicts[:7] == ["busy"] * 4 + ["quiet"] * 3
    # Nothing ran until three quiet readings after the busy spell: 7 polls, a minute apart.
    assert fake.clock.sleeps[:6] == [60.0] * 6
    assert [c[0] for c in calls] == ["one", "two"]
    assert all(verdict == "quiet" for _, verdict in calls)


def test_requests_record_the_gate_view_at_send_time(fake, store, client, make_gate):
    store.add_job("tests", "a.py", "/repo")
    fake.usage(times=3, active=1, queued=0)
    make_scheduler(store, make_gate(), client, fake, counting_plan(["one"], [])).run()
    (row,) = store.requests()
    assert row["gate_source"] == "livestats" and row["queue_at_send"] == 0 and row["active_at_send"] == 1


def test_killed_mid_job_resumes_without_repeating_finished_steps(fake, store, client, make_gate):
    job = store.add_job("docs", "a.py", "/repo")
    calls = []
    plans = counting_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, make_gate(), client, fake, plans).run()
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running", "pending"]

    # A fresh process: new scheduler and gate on the same database.
    done = make_scheduler(store, make_gate(), client, fake, plans).run()
    assert done == 2
    assert [c[0] for c in calls] == ["one", "two", "two", "three"]
    assert [s["status"] for s in store.steps(job.id)] == ["done", "done", "done"]
    assert store.get_job(job.id).status == "done"
    assert fake.chat_calls == 3  # step one's request was not sent again


def test_busy_answer_closes_the_gate_and_the_step_is_retried(fake, store, client, make_gate):
    job = store.add_job("review", "a.py", "/repo")
    fake.busy(1)  # first chat request gets 503
    calls = []
    done = make_scheduler(store, make_gate(), client, fake, counting_plan(["one"], calls)).run()
    assert done == 1 and store.get_job(job.id).status == "done"
    sources = [r["source"] for r in store.conn.execute("SELECT source FROM gate_readings ORDER BY id")]
    assert "own" in sources
    own = sources.index("own")
    assert sources[own + 1 : own + 4] == ["livestats"] * 3  # had to re-earn three quiet readings
    assert [r["status_code"] for r in store.requests()] == [503, 200]


def test_cancelled_job_stops_between_steps(fake, store, client, make_gate):
    job = store.add_job("tests", "a.py", "/repo")
    calls = []

    def cancel_self(ctx):
        calls.append("one")
        ctx.store.cancel_job(ctx.job.id)
        return "cancelled myself"

    plans = {k: [("one", cancel_self), ("two", lambda ctx: calls.append("two") or "")] for k in DEFAULT_PLANS}
    make_scheduler(store, make_gate(), client, fake, plans).run()
    assert calls == ["one"]
    assert store.get_job(job.id).status == "cancelled"


def test_survey_step_sends_the_target_and_saves_the_answer(fake, store, client, make_gate, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    job = store.add_job("tests", "src/calc.py", str(repo), note="cover negatives")
    fake.ok("- test add with negatives")
    make_scheduler(store, make_gate(), client, fake, None).run()
    (step,) = store.steps(job.id)
    assert step["name"] == "survey" and step["status"] == "done" and "negatives" in step["output"]
    sent = json.loads([r for r in fake.requests if r.url.path.endswith("/chat/completions")][0].content)
    assert "def add(a, b)" in sent["messages"][1]["content"]
    assert "cover negatives" in sent["messages"][1]["content"]
