from pathlib import Path

import pytest

from gpu_token_utilization import landing
from gpu_token_utilization.agent import BusyError
from gpu_token_utilization.fakedesktop import FakeClock, Step
from gpu_token_utilization.scheduler import DEFAULT_PLANS, Scheduler


def counting_plan(names, calls, crash_on=None, busy=None):
    """Steps that each ask the Desktop agent one question and record that they ran. `busy` maps a
    step name to how many times it reports a session that didn't finish in time first."""

    def make(name):
        def step(ctx):
            calls.append((name, ctx.step_name))
            if crash_on and crash_on.get(name):
                crash_on[name] -= 1
                raise KeyboardInterrupt  # stands in for the process being killed mid-step
            if busy and busy.get(name):
                busy[name] -= 1
                raise BusyError("BharatCode Desktop did not finish within 60 s")
            ctx.chat([{"role": "user", "content": name}])
            return f"{name} output"

        return step

    return {kind: [(n, make(n)) for n in names] for kind in ("tests", "docs", "review")}


def make_scheduler(store, desktop_fake, clock, plans, isolate=False, agent=None, **kwargs):
    return Scheduler(store, plans=plans, agent=agent or desktop_fake.agent(timeout=60), sleep=clock.sleep,
                     now=clock.utcnow, isolate=isolate, **kwargs)


def test_a_step_that_cannot_fit_the_budget_pauses_the_job_once_instead_of_spinning(desktop_fake, clock, store, job_repo):
    from gpu_token_utilization.budget import BudgetExceeded

    def too_big(ctx):
        raise BudgetExceeded("not enough token budget left for the review")  # as review() says before sending

    job = store.add_job("review", "big.py", job_repo)
    other = store.add_job("docs", "a.py", job_repo)
    calls, logs = [], []
    plans = {**counting_plan(["one"], calls), "review": [("review", too_big)]}  # the review step is the one that can't fit
    scheduler = make_scheduler(store, desktop_fake, clock, plans, log=logs.append)
    assert scheduler.run_job(job.id) == "paused"
    assert store.get_job(job.id).status == "paused" and store.get_job(other.id).status == "queued"
    assert sum("stopped, not enough token budget" in line for line in logs) == 1
    assert [s["status"] for s in store.steps(job.id)] == ["pending"]  # resumable
    assert scheduler.run_job(other.id) == "done"  # the other job is unaffected


def test_anything_else_that_breaks_a_step_fails_that_job_and_the_next_still_runs(desktop_fake, clock, store, job_repo):
    import xml.etree.ElementTree as ET

    def broken(ctx):
        raise ET.ParseError("junit.xml: no element found")  # a truncated report after a killed pytest

    job = store.add_job("tests", "a.py", job_repo)
    other = store.add_job("docs", "b.py", job_repo)
    calls, logs = [], []
    plans = {"tests": [("write", broken)], "docs": counting_plan(["one"], calls)["docs"]}
    scheduler = make_scheduler(store, desktop_fake, clock, plans, log=logs.append)
    assert scheduler.run_job(job.id) == "failed"
    assert store.steps(job.id)[0]["output"] == "ParseError: junit.xml: no element found"
    assert any("failed unexpectedly (ParseError" in line and "carry on" in line for line in logs)
    assert scheduler.run_job(other.id) == "done" and store.get_job(job.id).status == "failed"


def test_a_session_that_times_out_is_waited_out_and_the_step_tried_again_waiting_longer_each_time(
        desktop_fake, clock, store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    # The first two sessions never finish in time (100 polls of 2 s against a 5 s timeout); the rest answer.
    desktop_fake.calls = [[Step(polls=100)], [Step(polls=100)], [Step(text="done")]]
    session_clock = FakeClock()
    agent = desktop_fake.agent(timeout=5, poll=2, sleep=session_clock.sleep, clock=session_clock)
    calls, logs = [], []
    scheduler = make_scheduler(store, desktop_fake, clock, counting_plan(["one", "two"], calls), agent=agent,
                               log=logs.append)
    assert scheduler.run_job(job.id) == "done" and store.get_job(job.id).status == "done"
    assert clock.sleeps == [60.0, 120.0]  # a minute, then two: longer each time in a row
    assert [c[0] for c in calls] == ["one", "one", "one", "two"]
    sessions = list(desktop_fake.sessions.values())
    assert len(sessions) == 4 and [s["aborted"] for s in sessions] == [True, True, False, False]
    assert "BharatCode is busy; trying again in 60 s." in logs and "BharatCode is busy; trying again in 120 s." in logs
    assert any("BharatCode answered busy (BharatCode Desktop did not finish within 5 s)" in line for line in logs)


def test_killed_mid_job_resumes_without_repeating_finished_steps(desktop_fake, clock, store, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    calls = []
    plans = counting_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, desktop_fake, clock, plans).run_job(job.id)
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running", "pending"]

    # A fresh process: a new scheduler on the same database.
    assert make_scheduler(store, desktop_fake, clock, plans).run_job(job.id) == "done"
    assert [c[0] for c in calls] == ["one", "two", "two", "three"]
    assert [s["status"] for s in store.steps(job.id)] == ["done", "done", "done"]
    assert len(desktop_fake.sessions) == 3  # step one's session was not started again


def test_the_busy_wait_starts_over_once_a_step_gets_through(desktop_fake, clock, store, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    calls = []
    scheduler = make_scheduler(store, desktop_fake, clock, counting_plan(["one", "two"], calls, busy={"one": 1}))
    assert scheduler.run_job(job.id) == "done"
    assert [c[0] for c in calls] == ["one", "one", "two"]
    assert clock.sleeps == [60.0] and scheduler.busy_wait == 60.0  # back to a minute once the step got through


def test_cancelled_job_stops_between_steps(desktop_fake, clock, store, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    calls = []

    def cancel_self(ctx):
        calls.append("one")
        ctx.store.cancel_job(ctx.job.id)
        return "cancelled myself"

    plans = {k: [("one", cancel_self), ("two", lambda ctx: calls.append("two") or "")] for k in DEFAULT_PLANS}
    assert make_scheduler(store, desktop_fake, clock, plans).run_job(job.id) == "cancelled"
    assert calls == ["one"]
    assert store.get_job(job.id).status == "cancelled"


# --- git isolation ---------------------------------------------------------------

from conftest import git  # noqa: E402


def writing_plan(names, calls, crash_on=None):
    """Steps that each write a file in the job's workdir (and can crash halfway)."""

    def make(name):
        def step(ctx):
            calls.append(name)
            (ctx.workdir / f"{name}.txt").write_text(f"{name} for job {ctx.job.id}\n")
            if crash_on and crash_on.get(name):
                crash_on[name] -= 1
                (ctx.workdir / "half-done.txt").write_text("partial edit from a crashed step\n")
                raise KeyboardInterrupt
            return f"wrote {name}.txt"

        return step

    return {kind: [(n, make(n)) for n in names] for kind in ("tests", "docs", "review")}


def result_files(store, job_id):
    job = store.get_job(job_id)
    if not job.output_dir:
        return set()
    return set(landing.exported_files(Path(job.repo) / job.output_dir))  # RUN.md and manifest.json sit next to them


def test_job_works_on_its_own_copy_and_never_touches_main(desktop_fake, clock, store, git_repo):
    main_head = git(git_repo, "rev-parse", "main")
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    plans = writing_plan(["one", "two"], [])
    make_scheduler(store, desktop_fake, clock, plans, isolate=True).run_job(job.id)

    assert store.get_job(job.id).status == "done"
    # what it produced is in spare-gpu/<date>_<time>/
    folder = git_repo / store.get_job(job.id).output_dir
    assert result_files(store, job.id) == {"one.txt", "two.txt"}
    assert (folder / "one.txt").read_text().strip() == f"one for job {job.id}"
    # main: same commit, same checkout, nothing new in the working tree
    assert git(git_repo, "rev-parse", "main") == main_head
    assert git(git_repo, "branch", "--show-current") == "main"
    assert git(git_repo, "status", "--porcelain") == ""
    assert not (git_repo / "one.txt").exists()
    # the job's copy is gone and no branch was ever made
    assert not (git_repo / ".gtu" / "worktrees" / job.id).exists()
    assert git(git_repo, "branch", "--list", "gtu/*") == ""


def test_crash_mid_step_discards_partial_edits_and_resumes(desktop_fake, clock, store, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    calls = []
    plans = writing_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, desktop_fake, clock, plans, isolate=True).run_job(job.id)
    wt = git_repo / ".gtu" / "worktrees" / job.id
    assert (wt / "half-done.txt").exists()  # left behind by the "killed" process

    make_scheduler(store, desktop_fake, clock, plans, isolate=True).run_job(job.id)
    assert calls == ["one", "two", "two", "three"]
    assert result_files(store, job.id) == {"one.txt", "two.txt", "three.txt"}  # not half-done.txt


def test_unsaved_changes_are_what_the_job_starts_from(desktop_fake, clock, store, git_repo):
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    (git_repo / "src" / "calc.py").write_text("edited but not committed\n")
    seen = []

    def step(ctx):
        seen.append((ctx.workdir / "src" / "calc.py").read_text())
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, desktop_fake, clock, plans, isolate=True).run_job(job.id)
    assert store.get_job(job.id).status == "done" and seen == ["edited but not committed\n"]
    assert (git_repo / "src" / "calc.py").read_text() == "edited but not committed\n"  # still the user's


def test_opencode_config_is_never_committed(desktop_fake, clock, store, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))

    def step(ctx):
        (ctx.workdir / "opencode.json").write_text('{"permission": {}}')
        (ctx.workdir / "test_calc.py").write_text("def test_ok():\n    assert True\n")
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, desktop_fake, clock, plans, isolate=True).run_job(job.id)
    assert result_files(store, job.id) == {"test_calc.py"}  # never opencode.json


# --- run_job: one job, in front of whoever asked -------------------------------------------

def test_run_job_runs_only_that_job_to_the_end_and_leaves_the_rest_queued(desktop_fake, clock, store, job_repo):
    mine = store.add_job("docs", "a.py", job_repo)
    other = store.add_job("review", "b.py", job_repo)
    calls = []
    scheduler = make_scheduler(store, desktop_fake, clock, counting_plan(["one", "two"], calls))
    assert scheduler.run_job(mine.id) == "done"
    assert store.get_job(mine.id).status == "done" and store.get_job(other.id).status == "queued"
    assert [c[0] for c in calls] == ["one", "two"]


def test_run_job_waits_out_busy_answers_and_reports_a_failed_step(desktop_fake, clock, store, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    calls, logs = [], []
    scheduler = make_scheduler(store, desktop_fake, clock, counting_plan(["one"], calls, busy={"one": 1}),
                               log=logs.append)
    assert scheduler.run_job(job.id) == "done" and clock.sleeps == [60.0]

    def broken(ctx):
        raise ValueError("no element found")

    failing = store.add_job("docs", "b.py", job_repo)
    assert make_scheduler(store, desktop_fake, clock, {"docs": [("write", broken)]}).run_job(failing.id) == "failed"
    assert store.steps(failing.id)[0]["output"] == "ValueError: no element found"


def test_run_job_pauses_when_the_budget_is_used_up(desktop_fake, clock, store, job_repo):
    from gpu_token_utilization.budget import Budgets
    from gpu_token_utilization.scheduler import AGENT_ENDPOINT
    from gpu_token_utilization.store import RequestLog

    job = store.add_job("review", "a.py", job_repo)
    store.log_request(RequestLog(endpoint=AGENT_ENDPOINT, attempt=1, total_tokens=500, job_id=job.id))
    calls, logs = [], []
    scheduler = make_scheduler(store, desktop_fake, clock, counting_plan(["one"], calls), budgets=Budgets(per_job=400),
                               log=logs.append)
    assert scheduler.run_job(job.id) == "paused" and calls == []
    assert store.get_job(job.id).status == "paused" and not desktop_fake.sessions
    assert any("its token budget is used up (500 of 400)" in line for line in logs)


def test_run_job_ends_a_job_cancelled_while_it_runs(desktop_fake, clock, store, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    calls = []

    def first(ctx):
        calls.append("one")
        ctx.store.cancel_job(ctx.job.id)  # spare_cancel_work arrives while the first step runs
        return "one"

    plans = {"docs": [("one", first), ("two", lambda ctx: calls.append("two") or "two")]}
    scheduler = make_scheduler(store, desktop_fake, clock, plans,
                               should_stop=lambda: store.get_job(job.id).status == "cancelled")
    assert scheduler.run_job(job.id) == "cancelled" and calls == ["one"]
    assert store.get_job(job.id).status == "cancelled"


def test_run_job_stopped_between_steps_leaves_the_job_paused_and_resumable(desktop_fake, clock, store, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    asked = {"stop": False}

    def first(ctx):
        asked["stop"] = True  # a stop arrives while the first step runs
        return "one"

    plans = {"docs": [("one", first), ("two", lambda ctx: "two")]}
    scheduler = make_scheduler(store, desktop_fake, clock, plans, should_stop=lambda: asked["stop"])
    assert scheduler.run_job(job.id) == "paused" and scheduler.stopped
    assert [s["status"] for s in store.steps(job.id)] == ["done", "pending"]  # resumable
    assert make_scheduler(store, desktop_fake, clock, plans).run_job(job.id) == "done"
