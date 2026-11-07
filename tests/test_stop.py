"""`gtu stop`: halt a running `gtu start` cleanly; jobs end up paused and resumable."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import runstate, worktree
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler
from gpu_token_utilization.store import Store

from conftest import git

runner = CliRunner()


# --- run file and process helpers ----------------------------------------------------------

def test_run_file_and_stop_flag(tmp_path):
    assert runstate.running_pid(tmp_path) is None
    runstate.write_run(tmp_path)
    assert runstate.running_pid(tmp_path) == os.getpid()
    assert not runstate.stop_requested(tmp_path)
    runstate.request_stop(tmp_path)
    assert runstate.stop_requested(tmp_path)
    runstate.clear(tmp_path)
    assert runstate.running_pid(tmp_path) is None and not runstate.stop_requested(tmp_path)


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

def test_stop_while_waiting_out_a_busy_answer(fake, store, client, job_repo):
    job = store.add_job("review", "a.py", job_repo)
    fake.busy(100)  # BharatCode keeps answering busy
    checks = {"n": 0}

    def should_stop():
        checks["n"] += 1
        return checks["n"] > 5

    def review(ctx):
        ctx.chat([{"role": "user", "content": "review"}])
        return "x"

    s = Scheduler(store, client, plans={"review": [("x", review)]}, sleep=fake.clock.sleep,
                  isolate=False, should_stop=should_stop)
    assert s.run() == 0 and s.stopped
    assert sum(fake.clock.sleeps) < 10  # woke within seconds, not after the 60 s wait
    assert store.get_job(job.id).status == "paused"  # its step was put back and the job left resumable


def test_stop_mid_session_step_aborts_it_and_leaves_the_job_paused_and_clean(
        git_repo, store, client, fake, scenario):
    # a step that would take 60 s of polls (0.05 s each), writing half a file as it starts
    desktop = scenario(steps=[{"tokens": 100, "writes": {"tests/test_gtu_x.py": "half-written"}, "polls": 1200}])
    job = store.add_job("tests", "src/calc.py", str(git_repo))
    flag = threading.Event()
    threading.Timer(3.0, flag.set).start()

    def make(should_stop):
        return Scheduler(store, client, agent=desktop.agent(timeout=120, poll=0.05, sleep=time.sleep),
                         budgets=Budgets(), config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                         sleep=fake.clock.sleep, now=fake.clock.utcnow, should_stop=should_stop)

    started = time.monotonic()
    s = make(flag.is_set)
    s.run()
    assert time.monotonic() - started < 20  # the 60 s step was cut short
    assert next(iter(desktop.sessions.values()))["aborted"]  # and its session stopped
    assert s.stopped and store.get_job(job.id).status == "paused"  # paused, not failed
    assert [st["status"] for st in store.steps(job.id)] == ["pending", "pending"]
    wt = worktree.worktree_path(git_repo, job.id)
    assert not (wt / "tests" / "test_gtu_x.py").exists()  # unfinished edit dropped
    assert worktree.changed_files(wt, None) == []  # nothing half-done was kept

    # resume: a quick, good run this time
    scenario(steps=[{"tokens": 100, "writes": {"tests/test_gtu_x.py": "def test_ok():\n    assert True\n"}}])
    make(lambda: False).run()
    assert store.get_job(job.id).status == "done"


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


# --- the commands --------------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GTU_DB", str(tmp_path / ".gtu" / "gtu.db"))
    return tmp_path / ".gtu"


def wait_for_run_file(folder, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if runstate.running_pid(folder):
            return
        time.sleep(0.1)
    raise AssertionError("the stand-in gtu start never registered")


def test_stop_with_nothing_running(folder):
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0 and "Nothing is running." in result.output


def test_stop_with_nothing_running_still_pauses_crashed_jobs(folder, job_repo):
    with Store(folder / "gtu.db") as s:
        job = s.add_job("review", "a.py", job_repo)
        s.set_status(job.id, "running")
    result = runner.invoke(app, ["stop"])
    assert f"Paused interrupted job(s): {job.id}" in result.output


STANDIN = """
import json, os, pathlib, sys, time
folder = pathlib.Path(sys.argv[1]); folder.mkdir(parents=True, exist_ok=True)
(folder / "run.json").write_text(json.dumps({"pid": os.getpid()}))
obey = sys.argv[2] == "obey"
while not (obey and (folder / "stop").exists()):
    time.sleep(0.1)
"""


def test_stop_asks_first_and_the_run_stops_cleanly(folder):
    proc = subprocess.Popen([sys.executable, "-c", STANDIN, str(folder), "obey"])
    wait_for_run_file(folder)
    result = runner.invoke(app, ["stop", "--wait", "10"])
    assert result.exit_code == 0, result.output
    assert "stopped cleanly" in result.output
    assert proc.wait(timeout=5) == 0
    assert not (folder / "run.json").exists() and not (folder / "stop").exists()


def test_stop_kills_a_run_that_does_not_respond(folder):
    proc = subprocess.Popen([sys.executable, "-c", STANDIN, str(folder), "ignore"])
    wait_for_run_file(folder)
    result = runner.invoke(app, ["stop", "--wait", "1"])
    assert result.exit_code == 0, result.output
    assert "didn't stop within 1 s" in result.output and "killed" in result.output
    assert proc.wait(timeout=10) is not None


def test_start_refuses_to_run_twice(folder, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    runstate.write_run(folder)  # "another" gtu start: this test process, which is alive
    try:
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 1 and "already running" in result.output
    finally:
        runstate.clear(folder)
