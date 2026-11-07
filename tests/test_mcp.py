"""spare-gpu's MCP server: each of the five tools against fakes (BharatCode's API, live stats and
history, and a worker whose steps finish at once), missing-store handling, the real protocol (in
memory and over stdio, the way BharatCode Desktop runs it), and sharing the store between processes."""

import json
import subprocess
import sys
import textwrap

import anyio
import httpx
import mcp
import pytest

from gpu_token_utilization import actions, runstate, waits, worktree
from gpu_token_utilization.api import ApiKeyMissing
from gpu_token_utilization.mcp_server import SpareTools, build_server, db_path
from gpu_token_utilization.store import RequestLog, Store

from conftest import desktop_db, quick_plans, quick_tools

TOOLS = {"spare_delegate", "spare_waits", "spare_work_status", "spare_cancel_work", "spare_ask"}
PACK_TOOLS = {"scholar_search", "verify_refs", "build_report", "security_filter", "pack_cost"}  # only for `gtu mcp --pack`
REMOVED = {"spare_queue_work", "spare_last_night", "spare_run_when_free"}  # their jobs moved to `gtu` commands
KEY = "bc_live_test_key_not_real"


@pytest.fixture(autouse=True)
def no_db_override(monkeypatch):
    monkeypatch.delenv("GTU_DB", raising=False)


@pytest.fixture(autouse=True)
def fresh_waits():
    waits._cache.clear()
    yield
    waits._cache.clear()


@pytest.fixture
def tools(git_repo, fake):
    # wait figures from the fake: its live stats, and no queue-waits endpoint (so "no data")
    return quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()))


def readable(text: str) -> bool:
    return bool(text.strip()) and not text.lstrip().startswith(("{", "[")) and "Traceback" not in text


def queue(repo, kind, path, note=None):
    """Queue a job the way `gtu add` does; the server itself only hands work over via spare_delegate."""
    return actions.queue_job(lambda root: Store(db_path(root)), repo, kind, path, note).job


def finished_review(git_repo, tokens=355, wait_ms=12500):
    job = queue(git_repo, "review", "src")
    with Store(db_path(git_repo)) as store:
        step = store.add_step(job.id, "review")
        store.set_step(step, "running")
        store.set_step(step, "done", output="report written")
        store.log_request(RequestLog(endpoint="/chat/completions", attempt=1, status_code=200, prompt_tokens=300,
                                     completion_tokens=tokens - 300, total_tokens=tokens, job_id=job.id,
                                     step_id=step, queue_wait_ms=wait_ms))
        store.set_status(job.id, "done")
    return job


# --- spare_delegate: runs the job now, in front, and says how it went (routing is in test_routing_jobs) ---

def test_delegate_runs_the_job_now_in_the_same_store_as_the_cli(tools, git_repo):
    text = tools.delegate("tests", "src", note="cover negative numbers")
    assert readable(text) and text.startswith("Done: tests for src (job ") and "changed no files" in text
    with Store(git_repo / ".gtu" / "gtu.db") as store:  # exactly where `gtu add` puts it
        (job,) = store.list_jobs()
    assert (job.kind, job.target, job.note, job.status) == ("tests", "src", "cover negative numbers", "done")
    assert runstate.running_pid(git_repo / ".gtu") is None  # the run file is cleared once it's done


def test_delegate_reports_where_the_files_landed_and_narrates_progress(git_repo, fake):
    def write(ctx):
        (ctx.workdir / "tests" / "test_new.py").parent.mkdir(exist_ok=True)
        (ctx.workdir / "tests" / "test_new.py").write_text("def test_ok():\n    assert True\n")

    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()), plans=quick_plans(write))
    lines = []
    text = tools.delegate("tests", "src", progress=lines.append)
    assert text.startswith("Done: tests for src (job ") and "its edited files are in spare-gpu/" in text
    folder = text.split("its edited files are in ")[1].split("/ in this project")[0]
    assert (git_repo / folder / "tests" / "test_new.py").is_file()
    assert "Your files weren't touched" in text and "Desktop session named" in text
    assert not (git_repo / "tests").exists()
    assert any("step 1 of 2 (write) started" in line for line in lines) and any("finished;" in line for line in lines)


def test_delegate_without_desktop_or_a_key_leaves_the_job_queued_and_says_what_to_do(git_repo, fake):
    def no_worker(store, log, config):
        raise ApiKeyMissing("no BharatCode API key found")

    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()), make_worker=no_worker)
    text = tools.delegate("review", "src")
    assert text.startswith("Queued, not started: review for src") and "gtu setup" in text
    with Store(db_path(git_repo)) as store:
        assert store.list_jobs()[0].status == "queued"


def test_delegate_while_another_run_is_going_leaves_the_job_for_that_run(tools, git_repo, monkeypatch):
    monkeypatch.setattr(runstate, "running_pid", lambda folder: 99999)  # a `gtu start` in a terminal
    text = tools.delegate("docs", "src")
    assert text.startswith("Queued: docs for src") and "process 99999" in text
    with Store(db_path(git_repo)) as store:
        assert store.list_jobs()[0].status == "queued"


def test_a_cancelled_job_stops_and_says_so(git_repo, fake):
    def cancel_self(ctx):
        ctx.store.cancel_job(ctx.job.id)  # spare_cancel_work from another chat, mid-job

    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()), plans=quick_plans(cancel_self))
    text = tools.delegate("docs", "src")
    assert text.startswith("Cancelled: docs for src") and "nothing was copied" in text
    assert not (git_repo / ".gtu" / "worktrees").exists() or not any((git_repo / ".gtu" / "worktrees").iterdir())


def test_delegate_from_a_subfolder_resolves_the_repo_root(git_repo, fake):
    http = lambda: httpx.Client(transport=fake.transport())
    text = quick_tools(git_repo / "src", http).delegate("review", "src/calc.py")
    assert "Done: review for src/calc.py" in text
    assert (git_repo / ".gtu" / "gtu.db").exists() and not (git_repo / "src" / ".gtu").exists()


@pytest.mark.parametrize("path, problem", [("src/nope.py", "does not exist"), ("../elsewhere", "outside this repo")])
def test_delegate_refusals_are_explained(tools, git_repo, path, problem):
    (git_repo.parent / "elsewhere").mkdir()
    text = tools.delegate("docs", path)
    assert text.startswith("Didn't hand it over:") and problem in text


def test_delegate_with_unsaved_changes(tools, git_repo):
    (git_repo / "scratch.txt").write_text("wip")
    assert tools.delegate("review", "src").startswith("Done: review for src")


def test_delegate_in_a_folder_that_is_not_a_git_repo(tmp_path, fake):
    (tmp_path / "a.py").write_text("x = 1\n")
    http = lambda: httpx.Client(transport=fake.transport())
    text = quick_tools(tmp_path, http).delegate("review", ".")
    assert text.startswith("Done: review for ."), text


def test_delegate_notes_a_big_folder_that_is_not_a_git_repo(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 1)
    (tmp_path / "a.py").write_text("x = 1\n")
    http = lambda: httpx.Client(transport=fake.transport())
    text = quick_tools(tmp_path, http).delegate("review", ".")
    assert text.startswith("Done: review for .") and "isn't a git repo" in text


# --- spare_work_status ------------------------------------------------------------------------

def test_work_status_merges_queue_and_results(tools, git_repo, fake):
    done = finished_review(git_repo)
    queue(git_repo, "docs", "src/calc.py")
    text = tools.work_status()
    assert readable(text) and text.index("Waiting:") < text.index("Finished:")
    assert "docs src/calc.py, queued" in text and f"- {done.id}: review src, done" in text and "355 tokens" in text
    assert "Nothing is running now" in text


def test_work_status_with_nothing_yet(tools, git_repo):
    queue(git_repo, "review", "src")
    with Store(db_path(git_repo)) as store:
        store.conn.execute("DELETE FROM jobs")
        store.conn.commit()
    assert tools.work_status().startswith("Nothing queued, running or finished yet in ")


# --- spare_cancel_work ------------------------------------------------------------------------

def test_cancel_work_jobs_and_unknown_ids(tools, git_repo):
    job = queue(git_repo, "docs", "src")
    assert tools.cancel_work(f" {job.id} ").startswith(f"Cancelled job {job.id} (docs src)")
    again = tools.cancel_work(job.id)
    assert "Didn't cancel it" in again and "already cancelled" in again
    unknown = tools.cancel_work("zzzz")
    assert "no job with id 'zzzz'" in unknown and "spare_work_status" in unknown


# --- spare_waits -------------------------------------------------------------------------------

def test_waits_report_the_live_lane_and_say_when_they_cant_compare(tools, fake):
    fake.usage_busy(1)
    text = tools.waits()
    assert readable(text) and "Qwen 3.8 27B: no free slot (4/4 busy, 6 queued)." in text
    assert "DeepSeek V4.1 Flash: no data right now." in text and "Not enough data to compare" in text


def test_waits_need_no_store_and_create_none(git_repo, fake):
    text = SpareTools(git_repo, http=lambda: httpx.Client(transport=fake.transport())).waits()
    assert readable(text) and not (git_repo / ".gtu").exists()


# --- missing store ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [lambda t: t.work_status(), lambda t: t.cancel_work("1234abcd")])
def test_without_a_store_tools_explain_and_create_nothing(call, git_repo, tools):
    text = call(tools)
    assert "hasn't been used in this project yet" in text and "spare_delegate" in text
    assert not (git_repo / ".gtu").exists()


# --- the real protocol -----------------------------------------------------------------------

def test_protocol_offers_exactly_five_task_shaped_tools(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            return (await client.list_tools()).tools

    listed = {t.name: t for t in anyio.run(go)}
    assert set(listed) == TOOLS and not set(listed) & REMOVED
    delegate = listed["spare_delegate"]
    assert set(delegate.input_schema["properties"]["type"]["enum"]) == {"tests", "docs", "review", "fix"}
    assert delegate.input_schema["required"] == ["type", "path"]
    # each description names its example phrasings and steers away from its nearest neighbour
    for name, phrase, neighbour in [
        ("spare_work_status", "why hasn't my job started?", "spare_waits"),
        ("spare_cancel_work", "cancel the docs job", "spare_work_status"),
        ("spare_waits", "is now a good time to run a big refactor?", "spare_delegate"),
        ("spare_delegate", "write tests for src/pay.py on whichever model is free", "spare_ask"),
        ("spare_ask", "ask whichever model is free", "spare_delegate"),
    ]:
        description = " ".join(listed[name].description.split())
        assert phrase in description, name
        assert neighbour in description, name
        assert not any(gone in description for gone in REMOVED), name  # no description points at a removed tool
    assert listed["spare_work_status"].annotations.read_only_hint is True
    assert listed["spare_cancel_work"].annotations.destructive_hint is True


def test_the_packs_tools_exist_only_for_a_pack_server(tools, git_repo):
    async def go(pack):
        async with mcp.Client(build_server(git_repo, tools, pack=pack)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    assert set(anyio.run(go, False)) == TOOLS  # a plain install never pays for them
    listed = anyio.run(go, True)
    assert set(listed) == TOOLS | PACK_TOOLS
    assert "/deep-research" in listed["scholar_search"].description and "Google Scholar" in listed["scholar_search"].description
    assert listed["scholar_search"].annotations.read_only_hint is True
    assert "sources.json" in listed["verify_refs"].description and "DROP" in listed["verify_refs"].description
    assert listed["scholar_search"].input_schema["required"] == ["query"]
    assert "/security-review" in listed["security_filter"].description and listed["security_filter"].annotations.read_only_hint is True


def test_spare_waits_names_the_variant_per_role_and_pack_cost_reports_this_projects_tokens(
        tools, git_repo, fake, monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from gpu_token_utilization import stats

    fake.usage_busy(1)
    plain = tools.waits()
    assert "Pack roles" not in plain  # a plain server says nothing about the pack
    text = tools.waits(roles=True)
    assert "Pack roles, which variant to start now" in text
    for role in ("research-checker", "review-verifier", "security-verifier"):
        assert f"- {role}: start `{role}` (" in text  # unmeasured roles stay on the heavy model
    assert (git_repo / ".gtu" / "pack-usage.json").is_file()
    assert (git_repo / ".gtu" / ".gitignore").read_text(encoding="utf-8") == "*\n"  # never shows up as a change
    assert "no token figures" in tools.pack_cost()  # no Desktop data on this machine: said, never estimated
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    qwen = {"input": 500, "output": 40, "reasoning": 0, "cache": {"read": 8000, "write": 0}, "total": 8540}
    deepseek = {"input": 180, "output": 769, "reasoning": 0, "cache": {"read": 25600, "write": 0}, "total": 26549}
    db = desktop_db(tmp_path / "desktop.db", [(str(git_repo), now + 1000, "bharatcode/qwen-3.8-27b", qwen),
                                               (str(git_repo), now + 2000, "bharatcode/qwen-3.8-27b", qwen),
                                               (str(git_repo), now + 3000, "bharatcode/deepseek-v4.1-flash", deepseek),
                                               (str(tmp_path / "elsewhere"), now + 4000, "bharatcode/qwen-3.8-27b", qwen),
                                               (str(git_repo), now - 3_600_000, "bharatcode/qwen-3.8-27b", qwen)])
    monkeypatch.setenv(stats.DB_ENV, str(db))
    cost = tools.pack_cost()
    assert cost.startswith("Tokens used in this project's BharatCode Desktop sessions since the spare_waits call 0 min ago")
    assert "- DeepSeek V4.1 Flash: 26,549 tokens in 1 message(s) (180 input, 769 output, 25,600 read from cache)" in cost
    assert "- Qwen 3.8 27B: 17,080 tokens in 2 message(s)" in cost  # not the other project's, not the earlier one
    monkeypatch.setenv("GTU_SWITCH_SAFE", "review-verifier")  # measured (or marked) switch-safe: the router may move it
    routed = tools.waits(roles=True)
    assert "- review-verifier: start `" in routed and "- research-checker: start `research-checker` (" in routed


def test_a_role_marked_switch_safe_in_the_project_moves_when_the_heavy_model_is_queued(git_repo, fake, monkeypatch):
    from test_routing_jobs import wait_figures  # DeepSeek queued about 2 minutes, Qwen idle
    from gpu_token_utilization import router

    (git_repo / ".gtu").mkdir()
    (git_repo / ".gtu" / "switch-safe").write_text("review-verifier\n", encoding="utf-8")  # the project's own override
    tools = quick_tools(git_repo, wait_figures)
    text = tools.waits(roles=True)
    assert "- review-verifier: start `review-verifier-standard` (saves about" in text
    assert "- research-checker: start `research-checker` (research-checker jobs aren't marked switch-safe" in text
    assert router.State.load(git_repo / ".gtu").away_from is None  # a reading, not a routing: nothing saved
    again = tools.waits(roles=True)  # and every role is judged from the same state each time
    assert "- review-verifier: start `review-verifier-standard`" in again and "still routed away" not in again


def test_pack_cost_without_a_start_time_or_desktop_data_says_so(tools, git_repo):
    assert tools.pack_cost().startswith("No start time")
    (git_repo / ".gtu").mkdir(exist_ok=True)
    (git_repo / ".gtu" / "pack-usage.json").write_text('{"at": "2026-11-06T10:00:00+00:00"}', encoding="utf-8")
    assert "no token figures" in tools.pack_cost()  # conftest leaves no Desktop session data to read


def test_security_filter_through_the_tool(git_repo, fake):
    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()))
    text = tools.security_filter(json.dumps([
        {"file": "app.py", "line": 1, "description": "SQL injection via the id parameter.", "confidence": 0.9},
        {"file": "app.py", "line": 2, "description": "Missing rate limiting on login.", "confidence": 0.9},
    ]))
    assert text.startswith("Kept 1 of 2 candidate(s)") and "- DROPPED app.py:2: rate limiting" in text
    assert tools.security_filter("nope").startswith("Couldn't read the candidates:")


def test_scholar_search_and_verify_refs_through_the_tools(git_repo, fake):
    from test_scholar import registries

    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()), scholar_http=registries)
    text = tools.scholar_search("attention is all you need")
    assert text.startswith("1. Attention Is All You Need (2017)") and "arxiv: 1706.03762" in text
    assert tools.scholar_search("attention", limit=2, sources="arxiv").count("found in: arxiv") == 2
    assert "unknown source 'nope'" in tools.scholar_search("attention", sources="nope")
    folder = git_repo / "research" / "transformers"
    folder.mkdir(parents=True)
    (folder / "sources.json").write_text(json.dumps({"sources": [
        {"key": "lecun2015", "title": "Deep learning", "authors": ["Yann LeCun"], "doi": "10.1038/nature14539"},
        {"key": "fake", "title": "Made up", "doi": "10.1000/fake"}]}), encoding="utf-8")
    text = tools.verify_refs("research/transformers")
    assert "1 verified, 1 to drop" in text and "- DROP fake: no registry has DOI 10.1000/fake" in text
    assert json.loads((folder / "sources.json").read_text(encoding="utf-8"))["sources"][1]["verified"] is False
    assert tools.verify_refs("research/nothing").startswith("No sources.json in ")
    assert "outside this project" in tools.verify_refs("../elsewhere")
    (folder / "sections").mkdir()
    (folder / "sections" / "01-intro.md").write_text("## Intro\n\nDeep nets learn [lecun2015]; so it says [fake].\n",
                                                    encoding="utf-8")
    text = tools.build_report("research/transformers", "Transformers")
    assert "1 reference(s) listed" in text and "marked DROP by verify_refs, left out of References: fake" in text
    report = (folder / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Transformers\n\n## Intro") and "- [lecun2015] Yann LeCun" in report and "[fake] " not in report
    assert tools.build_report("research/nothing").startswith("No section files in ")
    assert "outside this project" in tools.build_report("../elsewhere")


def test_protocol_calls_return_plain_text(tools, git_repo):
    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            handed = await client.call_tool("spare_delegate", {"type": "review", "path": "src"})
            status = await client.call_tool("spare_work_status", {})
            return handed, status

    handed, status = anyio.run(go)
    assert not handed.is_error and readable(handed.content[0].text)
    assert "Done: review for src" in handed.content[0].text
    assert "review src, done" in status.content[0].text


@pytest.mark.skipif(sys.platform != "win32", reason="the stdio path BharatCode Desktop uses on Windows")
def test_gtu_mcp_over_stdio_like_bharatcode_runs_it(git_repo):
    # A real `gtu mcp` subprocess: nothing but protocol may reach stdout, on Windows too.
    # (A status call on a fresh repo needs no store, no key and no network.)
    params = mcp.StdioServerParameters(
        command=sys.executable,
        args=["-c", "from gpu_token_utilization.cli import app; app()", "mcp", "--dir", str(git_repo)],
    )

    async def go():
        with anyio.fail_after(60):
            async with mcp.Client(params) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                status = await client.call_tool("spare_work_status", {})
                return names, status.content[0].text

    names, text = anyio.run(go)
    assert names == TOOLS and "hasn't been used in this project yet" in text
    assert not (git_repo / ".gtu").exists()


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


def test_spare_delegate_sends_progress_while_the_job_runs(git_repo, fake, monkeypatch):
    """BharatCode Desktop resets its per-call timeout on every progress notification, so a job that
    takes minutes must keep them coming; here the heartbeat is made fast and the step slow."""
    from gpu_token_utilization import mcp_server

    monkeypatch.setattr(mcp_server, "HEARTBEAT_S", 0.05)

    def slow(ctx):
        import time
        time.sleep(0.3)

    tools = quick_tools(git_repo, lambda: httpx.Client(transport=fake.transport()), plans=quick_plans(slow))
    seen = []

    async def go():
        async with mcp.Client(build_server(git_repo, tools)) as client:
            async def on_progress(progress, total, message):
                seen.append((progress, message))
            return await client.call_tool("spare_delegate", {"type": "docs", "path": "src"}, progress_callback=on_progress)

    result = anyio.run(go)
    assert not result.is_error and result.content[0].text.startswith("Done: docs for src")
    assert len(seen) >= 2 and seen[-1][0] > seen[0][0]  # several heartbeats, counting up
    assert any("step 1 of 2 (write) started" in (message or "") for _, message in seen)  # carrying the latest line
