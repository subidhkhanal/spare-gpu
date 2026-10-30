"""spare-gpu's MCP server: each tool's text, missing-store handling, the real protocol (in memory
and over stdio, the way BharatCode's CLI runs it), and sharing the store between processes."""

import json
import subprocess
import sys
import textwrap
from datetime import datetime, time, timedelta, timezone

import anyio
import httpx
import mcp
import pytest

from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.mcp_server import SpareTools, build_server, db_path
from gpu_token_utilization.store import RequestLog, Store

from conftest import git

TOOLS = {"spare_add_job", "spare_list_jobs", "spare_report", "spare_why", "spare_cancel", "spare_start", "spare_stop"}


@pytest.fixture(autouse=True)
def no_db_override(monkeypatch):
    monkeypatch.delenv("GTU_DB", raising=False)


@pytest.fixture
def launched():
    """Repos the tools asked to start a runner for (no real runner is ever started here)."""
    return []


@pytest.fixture
def tools(git_repo, fake, launched):
    def gate(store):
        return Gate(store, GateConfig(window=(time(0, 0), time(0, 0))),
                    http=httpx.Client(transport=fake.transport()), now=fake.clock.utcnow)

    def launch(repo):
        launched.append(repo)
        return 4242

    return SpareTools(git_repo, make_gate=gate, launch=launch)


def readable(text: str) -> bool:
    return bool(text.strip()) and not text.lstrip().startswith(("{", "[")) and "Traceback" not in text


# --- each tool -------------------------------------------------------------------------------

def test_add_job_queues_into_the_same_store_as_the_cli(tools, git_repo):
    text = tools.add_job("tests", "src", note="cover negative numbers")
    assert readable(text) and "Queued a tests job for src" in text and "gtu/" in text
    with Store(git_repo / ".gtu" / "gtu.db") as store:  # exactly where `gtu add` puts it
        (job,) = store.list_jobs()
    assert (job.kind, job.target, job.note, job.status) == ("tests", "src", "cover negative numbers", "queued")
    assert job.id in text


def test_add_job_starts_the_background_runner(tools, git_repo, launched, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    text = tools.add_job("review", "src")
    assert launched == [git_repo.resolve()]
    assert "Started the runner in the background (process 4242)" in text and "spare_stop" in text


def test_add_job_without_a_key_queues_but_says_to_run_setup(tools, launched):
    text = tools.add_job("review", "src")
    assert "Queued a review job" in text and "no BharatCode API key" in text and "gtu setup" in text
    assert launched == []


def test_add_job_when_the_runner_is_already_going_starts_nothing(tools, git_repo, launched, monkeypatch):
    from gpu_token_utilization import runstate

    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    tools.add_job("review", "src")
    runstate.write_run(git_repo / ".gtu")  # this test process plays the running runner
    text = tools.add_job("docs", "src")
    assert "already running" in text and len(launched) == 1


def test_start_and_stop(tools, git_repo, launched, monkeypatch):
    assert "no jobs for this project yet" in tools.start()
    tools.add_job("review", "src", start=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    assert "1 job(s) waiting. Started the runner" in tools.start() and len(launched) == 1
    assert tools.stop().startswith("Nothing is running")
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
        store.cancel_job(job.id)
    assert tools.start().startswith("Nothing is queued")


def test_add_job_from_a_subfolder_resolves_the_repo_root(git_repo, fake):
    text = SpareTools(git_repo / "src", launch=lambda repo: 1).add_job("review", "src/calc.py")
    assert "Queued a review job for src/calc.py" in text
    assert (git_repo / ".gtu" / "gtu.db").exists() and not (git_repo / "src" / ".gtu").exists()


@pytest.mark.parametrize("path, problem", [("src/nope.py", "does not exist"), ("../elsewhere", "outside this repo")])
def test_add_job_refusals_are_explained(tools, git_repo, path, problem):
    (git_repo.parent / "elsewhere").mkdir()
    text = tools.add_job("docs", path)
    assert text.startswith("Didn't queue it:") and problem in text


def test_add_job_in_a_dirty_repo_says_what_to_do(tools, git_repo):
    (git_repo / "scratch.txt").write_text("wip")
    text = tools.add_job("review", "src")
    assert "Didn't queue it" in text and "uncommitted changes" in text
    assert not (git_repo / ".gtu").exists()  # refused before creating anything


def test_add_job_outside_any_git_repo(tmp_path):
    text = SpareTools(tmp_path, launch=lambda repo: 1).add_job("review", ".")
    assert "Didn't queue it" in text and "not inside a git repository" in text


def test_list_jobs_groups_by_status_with_ids_and_tokens(tools, git_repo):
    tools.add_job("review", "src")
    tools.add_job("docs", "src/calc.py")
    with Store(db_path(git_repo)) as store:
        done, waiting = store.list_jobs()
        store.set_status(done.id, "done")
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200,
                                     total_tokens=355, job_id=done.id))
    text = tools.list_jobs()
    assert readable(text) and "2 job(s)" in text
    assert text.index("Waiting:") < text.index("Finished:")
    assert f"- {waiting.id}: docs src/calc.py, queued" in text
    assert f"- {done.id}: review src, done" in text and "355 tokens" in text


def test_report_says_what_ran_and_what_it_made(tools, git_repo):
    tools.add_job("review", "src")
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
        step = store.add_step(job.id, "review")
        store.set_step(step, "running")
        store.set_step(step, "done", output="report written")
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, total_tokens=355,
                                     job_id=job.id, step_id=step, queue_wait_ms=12500))
        store.set_status(job.id, "done")
    text = tools.report()
    assert readable(text) and "1 job(s), 355 tokens" in text
    assert f"review src (job {job.id}): done" in text and "12.5 s" in text
    assert "| Job |" not in text  # plain text, not the Markdown table the CLI prints


def test_report_with_nothing_in_the_period(tools):
    tools.add_job("review", "src")
    with Store(db_path(tools.base)) as store:
        (job,) = store.list_jobs()
        store.conn.execute("UPDATE jobs SET updated_at = '2026-01-01T00:00:00+00:00'")
        store.conn.commit()
    assert tools.report().startswith("Nothing ran since")


def test_report_asked_at_midday_still_covers_last_night(tools, git_repo, monkeypatch):
    # found with BharatCode's agent at 13:05: "what did spare-gpu do last night?" answered
    # "Nothing ran since 30 Oct 12:00", because the default was the night that had just begun
    tools.add_job("review", "src")
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
        store.set_status(job.id, "done")
        stamp = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE jobs SET updated_at = ?", (stamp,))
        store.conn.commit()
    monkeypatch.setenv("GTU_NIGHT_START", (datetime.now() - timedelta(hours=1)).strftime("%H:%M"))
    assert f"review src (job {job.id}): done" in tools.report()


def test_why_gives_the_gates_decision_and_the_queue(tools, fake):
    tools.add_job("review", "src")
    fake.usage_busy(1)
    text = tools.why()
    assert readable(text) and text.startswith("Gate closed") and "busy" in text
    assert "1 job(s) waiting" in text and "gtu start" in text


def test_cancel_by_id_and_unknown_ids(tools, git_repo):
    tools.add_job("docs", "src")
    with Store(db_path(git_repo)) as store:
        (job,) = store.list_jobs()
    text = tools.cancel(f" {job.id} ")
    assert text.startswith(f"Cancelled job {job.id} (docs src)") and f"gtu/{job.id}" in text
    again = tools.cancel(job.id)
    assert "Didn't cancel it" in again and "already cancelled" in again
    assert "no job with id 'zzzz'" in tools.cancel("zzzz") and "spare_list_jobs" in tools.cancel("zzzz")


@pytest.mark.parametrize("call", [lambda t: t.list_jobs(), lambda t: t.report(), lambda t: t.why(),
                                  lambda t: t.cancel("1234abcd")])
def test_without_a_store_every_reading_tool_explains_and_creates_nothing(call, git_repo, tools):
    text = call(tools)
    assert "no jobs for this project yet" in text and "spare_add_job" in text
    assert not (git_repo / ".gtu").exists()


# --- the real protocol -----------------------------------------------------------------------

def test_protocol_lists_five_tools_with_descriptions_an_agent_can_choose_from(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            return (await client.list_tools()).tools

    listed = {t.name: t for t in anyio.run(go)}
    assert set(listed) == TOOLS
    add = listed["spare_add_job"]
    assert "tonight" in add.description and "later" in add.description.lower()
    assert set(add.input_schema["properties"]["type"]["enum"]) == {"tests", "docs", "review"}
    assert add.input_schema["required"] == ["type", "path"]
    assert "last night" in listed["spare_report"].description
    assert "why hasn't my job started" in listed["spare_why"].description
    assert listed["spare_list_jobs"].annotations.read_only_hint is True
    assert listed["spare_cancel"].annotations.destructive_hint is True


def test_protocol_calls_return_plain_text(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            added = await client.call_tool("spare_add_job", {"type": "review", "path": "src"})
            listed = await client.call_tool("spare_list_jobs", {})
            return added, listed

    added, listed = anyio.run(go)
    assert not added.is_error and readable(added.content[0].text) and "Queued a review job" in added.content[0].text
    assert "review src, queued" in listed.content[0].text


@pytest.mark.skipif(sys.platform != "win32", reason="the stdio path BharatCode's CLI uses on Windows")
def test_gtu_mcp_over_stdio_like_bharatcode_runs_it(git_repo):
    # A real `gtu mcp` subprocess: nothing but protocol may reach stdout, on Windows too.
    params = mcp.StdioServerParameters(
        command=sys.executable,
        args=["-c", "from gpu_token_utilization.cli import app; app()", "mcp", "--dir", str(git_repo)],
    )

    async def go():
        with anyio.fail_after(60):
            async with mcp.Client(params) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                added = await client.call_tool("spare_add_job", {"type": "docs", "path": "src/calc.py"})
                return names, added.content[0].text

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
