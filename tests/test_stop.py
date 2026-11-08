"""Stopping a job: a cancel from the chat ends a running job cleanly, without killing anything; jobs a
crashed server left running end up paused and resumable; and two servers never run jobs in the same
project at once."""

import json
import os
import subprocess
import sys

import httpx

from gpu_token_utilization import runstate, workers, worktree
from gpu_token_utilization.agent import BusyError
from gpu_token_utilization.fakedesktop import Step
from gpu_token_utilization.mcp_server import db_path
from gpu_token_utilization.scheduler import DEFAULT_PLANS, Scheduler
from gpu_token_utilization.store import Store

from conftest import git, quick_tools


def no_wait_figures() -> httpx.Client:
    """BharatCode's live stats, unavailable: every job stays on its preferred model."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))


# --- run file and process helpers ----------------------------------------------------------

def test_run_file_names_the_running_server(tmp_path):
    assert runstate.running_pid(tmp_path) is None
    runstate.write_run(tmp_path)
    assert runstate.running_pid(tmp_path) == os.getpid()
    assert runstate.read_run(tmp_path)["pid"] == os.getpid()
    runstate.clear(tmp_path)
    assert runstate.running_pid(tmp_path) is None and runstate.read_run(tmp_path) is None
    runstate.clear(tmp_path)  # clearing twice is fine


def test_a_stale_run_file_does_not_count(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (tmp_path / "run.json").write_text(json.dumps({"pid": int(done.stdout)}))
    assert runstate.running_pid(tmp_path) is None


def test_checking_liveness_never_kills_the_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(3):
            assert runstate.alive(child.pid)
        assert child.poll() is None  # on Windows, os.kill(pid, 0) would have terminated it
    finally:
        child.kill()
        child.wait()


# --- the scheduler -------------------------------------------------------------------------

def test_cancel_while_waiting_out_a_busy_answer(clock, store, job_repo):
    job = store.add_job("review", "a.py", job_repo)

    def review(ctx):
        raise BusyError("BharatCode Desktop did not finish within 60 s")  # Desktop keeps timing out

    def sleep(seconds):
        clock.sleep(seconds)
        if sum(clock.sleeps) >= 5:
            store.cancel_job(job.id)  # the user cancels five seconds into the wait

    s = Scheduler(store, plans={"review": [("x", review)]}, sleep=sleep, isolate=False,
                  should_stop=lambda: store.get_job(job.id).status == "cancelled")
    assert s.run_job(job.id) == "cancelled"
    assert sum(clock.sleeps) < 10  # woke within seconds, not after the 60 s wait
    assert store.get_job(job.id).status == "cancelled"


def test_cancel_mid_session_aborts_it_and_keeps_nothing(git_repo, store, clock, scenario):
    head = git(git_repo, "rev-parse", "HEAD")
    # a step that would take 1000 polls, writing half a file as it starts
    desktop = scenario(steps=[{"tokens": 100, "writes": {"tests/test_gtu_x.py": "half-written"}, "polls": 1000}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    polls = []

    def poll_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 5:
            store.cancel_job(job.id)  # spare_cancel_work, five polls into the session

    logs = []
    s = Scheduler(store, agent=desktop.agent(timeout=600, sleep=poll_sleep), sleep=clock.sleep, now=clock.utcnow,
                  log=logs.append, should_stop=lambda: store.get_job(job.id).status == "cancelled")
    assert s.run_job(job.id) == "cancelled"
    assert len(polls) < 10  # the 1000-poll step was cut short
    assert next(iter(desktop.sessions.values()))["aborted"]  # and its session stopped
    assert store.get_job(job.id).status == "cancelled"
    assert not worktree.worktree_path(git_repo, job.id).exists()  # its copy, half-written file and all, is gone
    assert not (git_repo / "spare-gpu").exists() and store.get_job(job.id).output_dir is None
    assert git(git_repo, "rev-parse", "main") == head and git(git_repo, "status", "--porcelain") == ""
    assert any("cancelled; its copy is removed and nothing was exported" in line for line in logs)


def test_tidy_makes_a_crashed_job_resumable(git_repo, store):
    job = store.add_job("docs", "src/calc.py", str(git_repo))
    step = store.add_step(job.id, "write")
    store.add_step(job.id, "verify")
    store.set_step(step, "running")
    store.set_status(job.id, "running")
    wt = worktree.create(git_repo, job.id)
    (wt / "src" / "calc.py").write_text("half-edited\n")
    assert runstate.tidy(store) == [job.id]
    assert store.get_job(job.id).status == "paused"
    assert [s["status"] for s in store.steps(job.id)] == ["pending", "pending"]
    assert (wt / "src" / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert runstate.tidy(store) == []  # nothing left to tidy


# --- through the MCP tools -----------------------------------------------------------------

def test_cancelling_a_running_job_from_the_chat_stops_it(git_repo, desktop_fake):
    desktop_fake.steps = [Step(tokens=100, writes={"tests/test_gtu_x.py": "half-written"}, polls=1000)]
    answers, polls = [], []

    def poll_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 5:  # the user cancels it from another chat while the session runs
            with Store(db_path(git_repo)) as store:
                (job,) = store.list_jobs()
            answers.append(tools.cancel_work(job.id))

    def make_worker(store, log, config):
        return workers.Worker(agent=desktop_fake.agent(timeout=600, sleep=poll_sleep))

    tools = quick_tools(git_repo, no_wait_figures, make_worker=make_worker, plans=DEFAULT_PLANS)
    text = tools.delegate("tests", "src/calc.py")
    assert answers and answers[0].startswith("Cancelled job ")
    assert "it was running; it stops within a few seconds" in answers[0]
    assert text.startswith("Cancelled: tests for src/calc.py (job ") and "nothing was copied to spare-gpu/" in text
    assert len(polls) < 10 and next(iter(desktop_fake.sessions.values()))["aborted"]
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
        assert job.status == "cancelled"
    assert not worktree.worktree_path(git_repo, job.id).exists() and not (git_repo / "spare-gpu").exists()
    assert runstate.running_pid(git_repo / ".gtu") is None  # the run file went with it


def test_delegate_first_pauses_jobs_a_crashed_server_left_running_and_drops_their_copy(git_repo):
    with Store(db_path(git_repo)) as store:
        crashed = store.add_job("review", "src/calc.py", str(git_repo))
        store.set_status(crashed.id, "running")
    wt = worktree.create(git_repo, crashed.id)
    (wt / "src" / "calc.py").write_text("half-edited\n")
    text = quick_tools(git_repo, no_wait_figures).delegate("docs", "src")
    assert text.startswith("Done: docs for src (job ")
    with Store(db_path(git_repo)) as store:
        assert store.get_job(crashed.id).status == "paused"
    assert not wt.exists()  # a paused job doesn't start again by itself, so its copy isn't kept
    assert (git_repo / "src" / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_delegate_never_runs_alongside_another_servers_job(git_repo):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        folder = git_repo / ".gtu"
        folder.mkdir(exist_ok=True)
        (folder / runstate.RUN_FILE).write_text(json.dumps({"pid": other.pid}))  # another Desktop window's server
        text = quick_tools(git_repo, no_wait_figures).delegate("docs", "src")
        assert text.startswith("Not started: another BharatCode Desktop window is running a spare-gpu job")
        assert f"(process {other.pid})" in text
        with Store(db_path(git_repo)) as store:
            (job,) = store.list_jobs()
            assert job.status == "cancelled"
        assert runstate.running_pid(folder) == other.pid  # its run file is left alone
        assert other.poll() is None  # and nothing was killed
    finally:
        other.kill()
        other.wait()
