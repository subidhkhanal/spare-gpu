import json
from pathlib import Path

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


def make_scheduler(store, gate, client, fake, plans, isolate=False):
    return Scheduler(store, gate, client, plans=plans, sleep=fake.clock.sleep, isolate=isolate)


def test_pauses_while_busy_and_resumes_when_quiet(fake, store, client, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
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


def test_requests_record_the_gate_view_at_send_time(fake, store, client, make_gate, job_repo):
    store.add_job("tests", "a.py", job_repo)
    fake.usage(times=3, active=1, queued=0)
    make_scheduler(store, make_gate(), client, fake, counting_plan(["one"], [])).run()
    (row,) = store.requests()
    assert row["gate_source"] == "livestats" and row["queue_at_send"] == 0 and row["active_at_send"] == 1


def test_killed_mid_job_resumes_without_repeating_finished_steps(fake, store, client, make_gate, job_repo):
    job = store.add_job("docs", "a.py", job_repo)
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


def test_busy_answer_closes_the_gate_and_the_step_is_retried(fake, store, client, make_gate, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    fake.busy(1)  # first chat request gets 503
    calls = []
    done = make_scheduler(store, make_gate(), client, fake, counting_plan(["one"], calls)).run()
    assert done == 1 and store.get_job(job.id).status == "done"
    sources = [r["source"] for r in store.conn.execute("SELECT source FROM gate_readings ORDER BY id")]
    assert "own" in sources
    own = sources.index("own")
    assert sources[own + 1 : own + 4] == ["livestats"] * 3  # had to re-earn three quiet readings
    assert [r["status_code"] for r in store.requests()] == [503, 200]


def test_cancelled_job_stops_between_steps(fake, store, client, make_gate, job_repo):
    job = store.add_job("tests", "a.py", job_repo)
    calls = []

    def cancel_self(ctx):
        calls.append("one")
        ctx.store.cancel_job(ctx.job.id)
        return "cancelled myself"

    plans = {k: [("one", cancel_self), ("two", lambda ctx: calls.append("two") or "")] for k in DEFAULT_PLANS}
    make_scheduler(store, make_gate(), client, fake, plans).run()
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


def test_job_works_on_its_own_copy_and_never_touches_main(fake, store, client, make_gate, git_repo):
    main_head = git(git_repo, "rev-parse", "main")
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    plans = writing_plan(["one", "two"], [])
    make_scheduler(store, make_gate(), client, fake, plans, isolate=True).run()

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


def test_crash_mid_step_discards_partial_edits_and_resumes(fake, store, client, make_gate, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    calls = []
    plans = writing_plan(["one", "two", "three"], calls, crash_on={"two": 1})
    with pytest.raises(KeyboardInterrupt):
        make_scheduler(store, make_gate(), client, fake, plans, isolate=True).run()
    wt = git_repo / ".gtu" / "worktrees" / job.id
    assert (wt / "half-done.txt").exists()  # left behind by the "killed" process

    make_scheduler(store, make_gate(), client, fake, plans, isolate=True).run()
    assert calls == ["one", "two", "two", "three"]
    assert result_files(store, job.id) == {"one.txt", "two.txt", "three.txt"}  # not half-done.txt


def test_unsaved_changes_are_what_the_job_starts_from(fake, store, client, make_gate, git_repo):
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    (git_repo / "src" / "calc.py").write_text("edited but not committed\n")
    seen = []

    def step(ctx):
        seen.append((ctx.workdir / "src" / "calc.py").read_text())
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, make_gate(), client, fake, plans, isolate=True).run()
    assert store.get_job(job.id).status == "done" and seen == ["edited but not committed\n"]
    assert (git_repo / "src" / "calc.py").read_text() == "edited but not committed\n"  # still the user's


def test_opencode_config_is_never_committed(fake, store, client, make_gate, git_repo):
    job = store.add_job("tests", "src/calc.py", str(git_repo))

    def step(ctx):
        (ctx.workdir / "opencode.json").write_text('{"permission": {}}')
        (ctx.workdir / "test_calc.py").write_text("def test_ok():\n    assert True\n")
        return "ok"

    plans = {k: [("write", step)] for k in ("tests", "docs", "review")}
    make_scheduler(store, make_gate(), client, fake, plans, isolate=True).run()
    assert result_files(store, job.id) == {"test_calc.py"}  # never opencode.json
