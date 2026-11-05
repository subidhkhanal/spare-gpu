"""spare-gpu's MCP server: each of the eight tools against fakes (BharatCode's API, live stats and
history, and the background processes), missing-store handling, the real protocol (in memory and
over stdio, the way BharatCode's CLI runs it), and sharing the store between processes."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from datetime import datetime, time, timedelta, timezone

import anyio
import httpx
import mcp
import pytest

from gpu_token_utilization import freerun, waits
from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.mcp_server import SpareTools, build_server, db_path
from gpu_token_utilization.store import RequestLog, Store

from conftest import git

TOOLS = {"spare_queue_work", "spare_delegate", "spare_waits", "spare_work_status", "spare_last_night",
         "spare_cancel_work", "spare_run_when_free", "spare_ask"}
KEY = "bc_live_test_key_not_real"


@pytest.fixture(autouse=True)
def no_db_override(monkeypatch):
    monkeypatch.delenv("GTU_DB", raising=False)


@pytest.fixture
def started():
    """What the tools asked to start in the background (nothing real is started here)."""
    return {"runner": [], "commands": []}


@pytest.fixture
def tools(git_repo, fake, started):
    def gate(store):
        return Gate(store, GateConfig(window=(time(0, 0), time(0, 0))),
                    http=httpx.Client(transport=fake.transport()), now=fake.clock.utcnow)

    def launch(repo):
        started["runner"].append(repo)
        return 4242

    def start_command(cmd):
        started["commands"].append(cmd.command)
        return None  # no real waiter

    # wait figures from the fake too: its live stats, and no queue-waits endpoint (so "no data")
    return SpareTools(git_repo, make_gate=gate, launch=launch, start_command=start_command,
                      http=lambda: httpx.Client(transport=fake.transport()))


def readable(text: str) -> bool:
    return bool(text.strip()) and not text.lstrip().startswith(("{", "[")) and "Traceback" not in text


def finished_review(git_repo, tools, tokens=355, wait_ms=12500):
    tools.queue_work("review", "src")
    with Store(db_path(git_repo)) as store:
        job = store.list_jobs()[-1]
        step = store.add_step(job.id, "review")
        store.set_step(step, "running")
        store.set_step(step, "done", output="report written")
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, prompt_tokens=300,
                                     completion_tokens=tokens - 300, total_tokens=tokens, job_id=job.id,
                                     step_id=step, queue_wait_ms=wait_ms))
        store.set_status(job.id, "done")
    return job


# --- spare_queue_work -------------------------------------------------------------------------

def test_queue_work_writes_to_the_same_store_as_the_cli(tools, git_repo):
    text = tools.queue_work("tests", "src", note="cover negative numbers")
    assert readable(text) and "Queued a tests job for src" in text and "spare-gpu/<date>_<time>/" in text
    with Store(git_repo / ".gtu" / "gtu.db") as store:  # exactly where `gtu add` puts it
        (job,) = store.list_jobs()
    assert (job.kind, job.target, job.note, job.status) == ("tests", "src", "cover negative numbers", "queued")


def test_queue_work_starts_the_background_runner_and_returns_at_once(tools, git_repo, started, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    text = tools.queue_work("review", "src")
    assert started["runner"] == [git_repo.resolve()]
    assert "Started spare-gpu's runner in the background (process 4242)" in text


def test_queue_work_without_a_key_queues_and_says_to_run_setup(tools, started):
    text = tools.queue_work("review", "src")
    assert "Queued a review job" in text and "no BharatCode API key" in text and "gtu setup" in text
    assert started["runner"] == []


def test_queue_work_when_the_runner_is_already_going_starts_nothing(tools, git_repo, started, monkeypatch):
    from gpu_token_utilization import runstate

    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    tools.queue_work("review", "src")
    runstate.write_run(git_repo / ".gtu")  # this test process plays the running runner
    assert "already going" in tools.queue_work("docs", "src") and len(started["runner"]) == 1


def test_queue_work_from_a_subfolder_resolves_the_repo_root(git_repo):
    text = SpareTools(git_repo / "src", launch=lambda repo: 1).queue_work("review", "src/calc.py")
    assert "Queued a review job for src/calc.py" in text
    assert (git_repo / ".gtu" / "gtu.db").exists() and not (git_repo / "src" / ".gtu").exists()


@pytest.mark.parametrize("path, problem", [("src/nope.py", "does not exist"), ("../elsewhere", "outside this repo")])
def test_queue_work_refusals_are_explained(tools, git_repo, path, problem):
    (git_repo.parent / "elsewhere").mkdir()
    text = tools.queue_work("docs", path)
    assert text.startswith("Didn't queue it:") and problem in text


def test_queue_work_in_a_dirty_repo_creates_nothing(tools, git_repo):
    (git_repo / "scratch.txt").write_text("wip")
    text = tools.queue_work("review", "src")
    assert "Didn't queue it" in text and "uncommitted changes" in text
    assert not (git_repo / ".gtu").exists()


def test_queue_work_outside_any_git_repo(tmp_path):
    text = SpareTools(tmp_path, launch=lambda repo: 1).queue_work("review", ".")
    assert "Didn't queue it" in text and "not inside a git repository" in text


# --- spare_work_status ------------------------------------------------------------------------

def test_work_status_merges_queue_results_and_why_it_waits(tools, git_repo, fake):
    done = finished_review(git_repo, tools)
    tools.queue_work("docs", "src/calc.py")
    fake.usage_busy(1)
    text = tools.work_status()
    assert readable(text) and text.index("Waiting:") < text.index("Finished:")
    assert "docs src/calc.py, queued" in text and f"- {done.id}: review src, done" in text and "355 tokens" in text
    assert "runner isn't going" in text and "Why it isn't running yet: Gate closed" in text


def test_work_status_lists_commands_waiting_for_capacity(tools):
    tools.run_when_free("npm run build")
    text = tools.work_status()
    assert "Commands to run when there's capacity:" in text and "`npm run build`, waiting" in text


def test_work_status_with_nothing_yet(tools):
    tools.queue_work("review", "src")
    with Store(db_path(tools.base)) as store:
        store.conn.execute("DELETE FROM jobs")
        store.conn.commit()
    assert tools.work_status().startswith("Nothing queued, running or finished yet in ")


# --- spare_last_night -------------------------------------------------------------------------

def test_last_night_says_what_ran_what_it_made_and_cost(tools, git_repo):
    job = finished_review(git_repo, tools)
    text = tools.last_night()
    assert readable(text) and "1 job(s), 355 tokens" in text
    assert f"review src (job {job.id}): done" in text and "12.5 s" in text and "| Job |" not in text


def test_last_night_asked_at_midday_still_covers_the_night(tools, git_repo, monkeypatch):
    # found with BharatCode's agent at 13:05: "what did spare-gpu do last night?" answered
    # "Nothing ran since 30 Oct 12:00", because the default was the night that had just begun
    job = finished_review(git_repo, tools)
    with Store(db_path(git_repo)) as store:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE jobs SET updated_at = ?", (stamp,))
        store.conn.execute("UPDATE job_steps SET started_at = ?, finished_at = ?", (stamp, stamp))
        store.conn.commit()
    monkeypatch.setenv("GTU_NIGHT_START", (datetime.now() - timedelta(hours=1)).strftime("%H:%M"))
    assert f"review src (job {job.id}): done" in tools.last_night()


def test_last_night_with_nothing_in_the_period(tools, git_repo):
    tools.queue_work("review", "src")
    with Store(db_path(git_repo)) as store:
        store.conn.execute("UPDATE jobs SET updated_at = '2026-01-01T00:00:00+00:00'")
        store.conn.commit()
    assert tools.last_night().startswith("Nothing ran since")


# --- spare_cancel_work ------------------------------------------------------------------------

def test_cancel_work_jobs_and_unknown_ids(tools, git_repo):
    tools.queue_work("docs", "src")
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
    assert tools.cancel_work(f" {job.id} ").startswith(f"Cancelled job {job.id} (docs src)")
    again = tools.cancel_work(job.id)
    assert "Didn't cancel it" in again and "already cancelled" in again
    unknown = tools.cancel_work("zzzz")
    assert "no job with id 'zzzz'" in unknown and "spare_work_status" in unknown


def test_cancel_work_cancels_a_waiting_command(tools, git_repo):
    tools.run_when_free("npm run build")
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
    assert tools.cancel_work(cmd.id) == f"Cancelled command {cmd.id} (`npm run build`)."
    with Store(db_path(git_repo)) as store:
        assert store.get_command(cmd.id).status == "cancelled"


# --- spare_waits -------------------------------------------------------------------------------

@pytest.fixture
def fresh_waits():
    waits._cache.clear()
    yield
    waits._cache.clear()


def test_waits_report_the_live_lane_and_say_when_they_cant_compare(tools, fake, fresh_waits):
    fake.usage_busy(1)
    text = tools.waits()
    assert readable(text) and "Qwen 3.8 27B: no free slot (4/4 busy, 6 queued)." in text
    assert "DeepSeek V4.1 Flash: no data right now." in text and "Not enough data to compare" in text


def test_waits_need_no_store_and_create_none(git_repo, fake, fresh_waits):
    gate = lambda store: Gate(store, GateConfig(window=(time(0, 0), time(0, 0))),
                              http=httpx.Client(transport=fake.transport()), now=fake.clock.utcnow)
    text = SpareTools(git_repo, make_gate=gate, http=lambda: httpx.Client(transport=fake.transport())).waits()
    assert readable(text) and not (git_repo / ".gtu").exists()


# --- spare_run_when_free ----------------------------------------------------------------------

def test_run_when_free_queues_and_returns_at_once(tools, git_repo, started):
    text = tools.run_when_free("  pytest -x tests  ")
    assert started["commands"] == ["pytest -x tests"]
    assert "Will run `pytest -x tests`" in text and "spare_cancel_work" in text
    assert "Didn't queue it: no command given" in tools.run_when_free("   ")


@pytest.mark.parametrize("command", [
    "pytest", "python -m pytest -x tests", "py -m pytest", "npm test", "npm.cmd run build", "go test ./...",
    "cargo test", "make check", "bharatcode run 'add docstrings to src/utils'", "PYTEST -q",
])
def test_allowed_commands(command):
    assert freerun.check_command(command)


@pytest.mark.parametrize("command, why", [
    ("del /s /q src", "isn't on the list"),
    ("rm -rf .", "isn't on the list"),
    ("git push --force", "isn't on the list"),
    ("git reset --hard", "isn't on the list"),
    ("powershell -Command Remove-Item -Recurse .", "isn't on the list"),
    ("python -c \"import shutil; shutil.rmtree('src')\"", "isn't on the list|shell characters"),
    ("python -c \"import shutil, os\"", "isn't on the list"),
    ("python evil.py", "isn't on the list"),
    ("curl https://example.com/x.sh", "isn't on the list"),
    ("pytest && del /q src", "shell characters"),
    ("pytest & rd /s /q src", "shell characters"),
    ("npm test | more", "shell characters"),
    ("pytest > C:\\Windows\\out.txt", "shell characters"),
    ("pytest; rm -rf .", "shell characters"),
    ("bharatcode run \"fix it & del src\"", "shell characters"),  # .cmd programs go through cmd.exe
    ("npm run %COMSPEC%", "shell characters"),
    ("C:\\evil\\pytest.exe", "not a path"),
    ("..\\tools\\pytest", "not a path"),
    ("pytest 'unclosed", "unclosed quote"),
])
def test_destructive_or_chained_commands_are_refused_in_code(command, why, tools, git_repo, started):
    with pytest.raises(freerun.FreeRunError, match=why):
        freerun.check_command(command)
    text = tools.run_when_free(command)  # the tool refuses it before recording or starting anything
    assert text.startswith("Didn't queue it:") and started["commands"] == []
    assert not (git_repo / ".gtu").exists() or not Store(db_path(git_repo)).list_commands()


def test_the_allowlist_can_be_extended_on_purpose(monkeypatch):
    with pytest.raises(freerun.FreeRunError):
        freerun.check_command("python scripts/refactor.py")
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python scripts/refactor.py, node tools/migrate.js")
    assert freerun.check_command("python scripts/refactor.py --dry-run")
    assert freerun.check_command("node tools/migrate.js")
    with pytest.raises(freerun.FreeRunError, match="shell characters"):  # extensions don't relax the rest
        freerun.check_command("python scripts/refactor.py && del x")


def test_a_command_disallowed_after_queueing_is_not_run(tools, git_repo, fake, monkeypatch):
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python scripts/refactor.py")
    tools.run_when_free("python scripts/refactor.py")
    monkeypatch.delenv("GTU_RUN_ALLOWED")  # the allowlist changed while it waited
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        said = []
        assert freerun.wait_and_run(store, cmd.id, sleep=fake.clock.sleep, say=said.append) is None
        assert store.get_command(cmd.id).status == "failed" and "Not running it" in said[-1]


def test_the_waiter_runs_the_command_once_the_gate_opens(tools, git_repo, fake, monkeypatch):
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python -c")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    tools.run_when_free("python -c \"open('ran.txt', 'w').write('yes')\"")
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        fake.usage_busy(2)  # busy for a while, then quiet
        said = []
        gate = lambda s: Gate(s, GateConfig(window=(time(0, 0), time(0, 0))),
                              http=httpx.Client(transport=fake.transport()), now=fake.clock.utcnow)
        code = freerun.wait_and_run(store, cmd.id, make_gate=gate, sleep=fake.clock.sleep, say=said.append)
        assert code == 0 and (git_repo / "ran.txt").read_text() == "yes"
        assert any(line.startswith("Gate closed") for line in said)
        done = store.get_command(cmd.id)
        assert done.status == "done" and done.exit_code == 0 and done.started_at and done.finished_at


def test_a_command_cancelled_while_waiting_never_runs(tools, git_repo, fake):
    tools.run_when_free("pytest -x")
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        store.cancel_command(cmd.id)
        assert freerun.wait_and_run(store, cmd.id, sleep=fake.clock.sleep, say=lambda m: None) is None
    assert not (git_repo / "ran.txt").exists()


# --- missing store ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [lambda t: t.work_status(), lambda t: t.last_night(),
                                  lambda t: t.cancel_work("1234abcd")])
def test_without_a_store_tools_explain_and_create_nothing(call, git_repo, tools):
    text = call(tools)
    assert "hasn't been used in this project yet" in text and "spare_queue_work" in text
    assert not (git_repo / ".gtu").exists()


# --- the real protocol -----------------------------------------------------------------------

def test_protocol_offers_exactly_eight_task_shaped_tools(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            return (await client.list_tools()).tools

    listed = {t.name: t for t in anyio.run(go)}
    assert set(listed) == TOOLS
    queue = listed["spare_queue_work"]
    assert set(queue.input_schema["properties"]["type"]["enum"]) == {"tests", "docs", "review"}
    assert queue.input_schema["required"] == ["type", "path"]
    # each description names its example phrasings and steers away from its nearest neighbour
    for name, phrase, neighbour in [
        ("spare_queue_work", "queue tests for the payments module", "spare_run_when_free"),
        ("spare_work_status", "why hasn't my job started?", "spare_last_night"),
        ("spare_last_night", "what did spare-gpu do last night?", "spare_work_status"),
        ("spare_cancel_work", "cancel the docs job", "spare_work_status"),
        ("spare_waits", "is now a good time to run a big refactor?", "spare_delegate"),
        ("spare_delegate", "write tests for src/pay.py on whichever model is free", "spare_queue_work"),
        ("spare_run_when_free", "run the test suite when BharatCode is free", "spare_queue_work"),
        ("spare_ask", "ask whichever model is free", "spare_delegate"),
    ]:
        description = " ".join(listed[name].description.split())
        assert phrase in description, name
        assert neighbour is None or neighbour in description, name
    assert listed["spare_work_status"].annotations.read_only_hint is True
    assert listed["spare_cancel_work"].annotations.destructive_hint is True


def test_protocol_calls_return_plain_text(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            queued = await client.call_tool("spare_queue_work", {"type": "review", "path": "src"})
            status = await client.call_tool("spare_work_status", {})
            return queued, status

    queued, status = anyio.run(go)
    assert not queued.is_error and readable(queued.content[0].text) and "Queued a review job" in queued.content[0].text
    assert "review src, queued" in status.content[0].text


@pytest.mark.skipif(sys.platform != "win32", reason="the stdio path BharatCode's CLI uses on Windows")
def test_gtu_mcp_over_stdio_like_bharatcode_runs_it(git_repo):
    # A real `gtu mcp` subprocess: nothing but protocol may reach stdout, on Windows too.
    # (No API key here, so queueing can't start a real runner.)
    params = mcp.StdioServerParameters(
        command=sys.executable,
        args=["-c", "from gpu_token_utilization.cli import app; app()", "mcp", "--dir", str(git_repo)],
    )

    async def go():
        with anyio.fail_after(60):
            async with mcp.Client(params) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                queued = await client.call_tool("spare_queue_work", {"type": "docs", "path": "src/calc.py"})
                return names, queued.content[0].text

    names, text = anyio.run(go)
    assert names == TOOLS and "Queued a docs job for src/calc.py" in text
    assert (git_repo / ".gtu" / "gtu.db").exists()


# --- several processes sharing the store ---------------------------------------------------

WORKER = textwrap.dedent("""
    import sys
    from gpu_token_utilization.store import Store
    path, n = sys.argv[1], int(sys.argv[2])
    with Store(path) as store:
        for i in range(n):
            job = store.add_job("review", f"src/{i}.py", "/repo-" + sys.argv[3])
            store.set_status(job.id, "running")
            store.add_step(job.id, "review")
            store.list_jobs()
            store.set_status(job.id, "done")
""")


def test_four_processes_writing_at_once_lose_nothing(tmp_path):
    path = tmp_path / ".gtu" / "gtu.db"
    Store(path).close()
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(path), "40", str(w)],
                              stderr=subprocess.PIPE, text=True) for w in range(4)]
    errors = [p.communicate(timeout=120)[1] for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
    with Store(path) as store:
        jobs = store.list_jobs()
        assert len(jobs) == 160 and all(j.status == "done" for j in jobs)
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


CANCELLER = textwrap.dedent("""
    import json, sys
    from gpu_token_utilization.store import Store, StoreError
    won = []
    with Store(sys.argv[1]) as store:
        for job_id in json.loads(sys.argv[2]):
            try:
                store.cancel_job(job_id)
                won.append(job_id)
            except StoreError:
                pass
    print(json.dumps(won))
""")
FINISHER = textwrap.dedent("""
    import json, sys
    from gpu_token_utilization.store import Store
    with Store(sys.argv[1]) as store:
        for job_id in json.loads(sys.argv[2]):
            store.set_status(job_id, "done")
""")


def test_cancel_racing_a_runner_finishing_never_leaves_a_contradiction(tmp_path):
    path = tmp_path / ".gtu" / "gtu.db"
    with Store(path) as store:
        ids = [store.add_job("tests", f"t{i}.py", "/repo").id for i in range(60)]
        for job_id in ids:
            store.set_status(job_id, "running")
    arg = json.dumps(ids)
    cancel = subprocess.Popen([sys.executable, "-c", CANCELLER, str(path), arg], stdout=subprocess.PIPE, text=True)
    finish = subprocess.Popen([sys.executable, "-c", FINISHER, str(path), arg], stderr=subprocess.PIPE, text=True)
    won = set(json.loads(cancel.communicate(timeout=120)[0]))
    assert finish.wait(timeout=120) == 0, finish.stderr.read()
    with Store(path) as store:
        final = {j.id: j.status for j in store.list_jobs()}
    # a successful cancel always sticks; a refused one means the runner had already finished it
    assert all(final[i] == ("cancelled" if i in won else "done") for i in ids)


def test_a_cancelled_job_stays_cancelled_when_the_runner_reports_late(store):
    job = store.add_job("docs", "a.py", "/repo")
    store.set_status(job.id, "running")
    store.cancel_job(job.id)
    assert store.set_status(job.id, "done").status == "cancelled"
