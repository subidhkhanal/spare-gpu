import json
from pathlib import Path

import pytest

from gpu_token_utilization.scheduler import DEFAULT_PLANS, Scheduler


def counting_plan(names, calls, crash_on=None):
    """Steps that each send one chat request and record that they ran."""

    def make(name):
        def step(ctx):
            calls.append((name, ctx.step_name))
            if crash_on and crash_on.get(name):
                crash_on[name] -= 1
                raise KeyboardInterrupt  # stands in for the process being killed mid-step
            ctx.chat([{"role": "user", "content": name}])
            return f"{name} output"

        return step

    return {kind: [(n, make(n)) for n in names] for kind in ("tests", "docs", "review")}


def make_scheduler(store, client, fake, plans, isolate=False, **kwargs):
    return Scheduler(store, client, plans=plans, sleep=fake.clock.sleep, isolate=isolate, **kwargs)


def test_a_step_that_cannot_fit_the_budget_pauses_the_job_once_instead_of_spinning(fake, store, client, job_repo):
    from gpu_token_utilization.budget import BudgetExceeded

    def too_big(ctx):
        raise BudgetExceeded("not enough token budget left for the review")  # as review() says before sending

    job = store.add_job("review", "big.py", job_repo)
    other = store.add_job("docs", "a.py", job_repo)
    calls, logs = [], []
    plans = {**counting_plan(["one"], calls), "review": [("review", too_big)]}  # the review step is the one that can't fit
    scheduler = Scheduler(store, client, plans=plans, sleep=fake.clock.sleep, isolate=False, log=logs.append)
    assert scheduler.run() == 1  # the docs job ran; the review was paused once and left alone
    assert store.get_job(job.id).status == "paused" and store.get_job(other.id).status == "done"
    assert sum("paused, not enough token budget" in line for line in logs) == 1
    assert any("Nothing left that fits the token budget" in line for line in logs)


def test_anything_else_that_breaks_a_step_fails_that_job_and_the_runner_carries_on(fake, store, client, job_repo):
    import xml.etree.ElementTree as ET

    def broken(ctx):
        raise ET.ParseError("junit.xml: no element found")  # a truncated report after a killed pytest

    job = store.add_job("tests", "a.py", job_repo)
    other = store.add_job("docs", "b.py", job_repo)
    calls, logs = [], []
    plans = {"tests": [("write", broken)], "docs": counting_plan(["one"], calls)["docs"]}
    scheduler = Scheduler(store, client, plans=plans, sleep=fake.clock.sleep, isolate=False, log=logs.append)
    assert scheduler.run() == 1
    assert store.get_job(job.id).status == "failed" and store.get_job(other.id).status == "done"
    assert store.steps(job.id)[0]["output"] == "ParseError: junit.xml: no element found"
    assert any("failed unexpectedly (ParseError" in line and "carry on" in line for line in logs)


def test_a_busy_answer_is_waited_out_and_the_step_tried_again_waiting_longer_each_time(fake, store, client, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    fake.busy(2)  # the first two chat requests get 503, then the default reply
    calls, logs = [], []
    done = make_scheduler(store, client, fake, counting_plan(["one", "two"], calls), log=logs.append).run()
    assert done == 2 and store.get_job(job.id).status == "done"
    assert [r["status_code"] for r in store.requests()] == [503, 503, 200, 200]
    assert fake.clock.sleeps == [60.0, 120.0]  # a minute, then two: longer each time in a row
    assert [c[0] for c in calls] == ["one", "one", "one", "two"]
    assert "BharatCode is busy; trying again in 60 s." in logs and "BharatCode is busy; trying again in 120 s." in logs


def test_killed_mid_job_resumes_without_repeating_finished_steps(fake, store, client, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
    calls = []
    plans = counting_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, client, fake, plans).run()
    assert [s["status"] for s in store.steps(job.id)] == ["done", "running", "pending"]

    # A fresh process: new scheduler and gate on the same database.
    done = make_scheduler(store, client, fake, plans).run()
    assert done == 2
    assert [c[0] for c in calls] == ["one", "two", "two", "three"]
    assert [s["status"] for s in store.steps(job.id)] == ["done", "done", "done"]
    assert store.get_job(job.id).status == "done"
    assert fake.chat_calls == 3  # step one's request was not sent again


def test_the_busy_wait_starts_over_once_a_step_gets_through(fake, store, client, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    fake.busy(1)  # the first chat request gets 503; the default reply answers the rest
    calls = []
    scheduler = make_scheduler(store, client, fake, counting_plan(["one", "two"], calls))
    assert scheduler.run() == 2 and store.get_job(job.id).status == "done"
    assert [r["status_code"] for r in store.requests()] == [503, 200, 200]
    assert fake.clock.sleeps == [60.0] and scheduler.busy_wait == 60.0  # back to a minute once the step got through


def test_cancelled_job_stops_between_steps(fake, store, client, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    calls = []

    def cancel_self(ctx):
        calls.append("one")
        ctx.store.cancel_job(ctx.job.id)
        return "cancelled myself"

    plans = {k: [("one", cancel_self), ("two", lambda ctx: calls.append("two") or "")] for k in DEFAULT_PLANS}
    make_scheduler(store, client, fake, plans).run()
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
    root = Path(job.repo) / job.output_dir
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_job_works_on_its_own_copy_and_never_touches_main(fake, store, client, git_repo):
    main_head = git(git_repo, "rev-parse", "main")
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    plans = writing_plan(["one", "two"], [])
    make_scheduler(store, client, fake, plans, isolate=True).run()

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


def test_crash_mid_step_discards_partial_edits_and_resumes(fake, store, client, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    calls = []
    plans = writing_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, client, fake, plans, isolate=True).run()
    wt = git_repo / ".gtu" / "worktrees" / job.id
    assert (wt / "half-done.txt").exists()  # left behind by the "killed" process

    make_scheduler(store, client, fake, plans, isolate=True).run()
    assert calls == ["one", "two", "two", "three"]
    assert result_files(store, job.id) == {"one.txt", "two.txt", "three.txt"}  # not half-done.txt


def test_unsaved_changes_are_what_the_job_starts_from(fake, store, client, git_repo):
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    (git_repo / "src" / "calc.py").write_text("edited but not committed\n")
    seen = []

    def step(ctx):
        seen.append((ctx.workdir / "src" / "calc.py").read_text())
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, client, fake, plans, isolate=True).run()
    assert store.get_job(job.id).status == "done" and seen == ["edited but not committed\n"]
    assert (git_repo / "src" / "calc.py").read_text() == "edited but not committed\n"  # still the user's


def test_opencode_config_is_never_committed(fake, store, client, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))

    def step(ctx):
        (ctx.workdir / "opencode.json").write_text('{"permission": {}}')
        (ctx.workdir / "test_calc.py").write_text("def test_ok():\n    assert True\n")
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, client, fake, plans, isolate=True).run()
    assert result_files(store, job.id) == {"test_calc.py"}  # never opencode.json
