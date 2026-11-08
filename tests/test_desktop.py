"""Running job steps through BharatCode Desktop's local server (against a fake one)."""

import json

import httpx
import pytest

from gpu_token_utilization import desktop
from gpu_token_utilization.desktop import Connection, DesktopAgent, DesktopNotFound
from gpu_token_utilization.fakedesktop import PASSWORD, FakeDesktop, Step

URL = "http://127.0.0.1:4096"


@pytest.fixture
def fake():
    return FakeDesktop()


def agent(fake, **kw):
    return DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=fake.transport(), sleep=lambda s: None, **kw)


# --- finding the server ---------------------------------------------------------------------

def test_discover_needs_desktops_password(fake):
    with pytest.raises(DesktopNotFound, match="wasn't started by BharatCode Desktop"):
        desktop.discover(env={}, transport=fake.transport())


def test_discover_uses_the_url_it_was_given(fake):
    env = {desktop.PASSWORD_ENV: PASSWORD, desktop.URL_ENV: URL + "/"}
    conn = desktop.discover(env=env, candidates=lambda: pytest.fail("shouldn't search"), transport=fake.transport())
    assert conn.url == URL and conn.username == "opencode"


def test_discover_tries_ancestor_ports_until_one_is_healthy(fake):
    tried = []
    bad = FakeDesktop(password="someone-else")  # the first candidate isn't Desktop's server

    class Switch(httpx.BaseTransport):
        def handle_request(self, request):
            tried.append(str(request.url))
            return (fake if request.url.port == 4096 else bad).transport().handle_request(request)

    env = {desktop.PASSWORD_ENV: PASSWORD}
    conn = desktop.discover(env=env, candidates=lambda: ["http://127.0.0.1:5000", URL], transport=Switch())
    assert conn.url == URL and len(tried) == 2


def test_discover_says_what_it_tried(fake):
    fake.healthy = False
    env = {desktop.PASSWORD_ENV: PASSWORD, desktop.URL_ENV: URL}
    with pytest.raises(DesktopNotFound, match="couldn't reach.*4096"):
        desktop.discover(env=env, transport=fake.transport())


# The real process tree on 4 Nov: Desktop's main process 11792, its server process 3972 (whose
# listening socket netstat lists as process 0), its network process 20996, spare-gpu's MCP
# server (26752 -> 27328) and the runner it started (21576 -> 28032). 555 is unrelated.
PARENTS = {11792: 6084, 3972: 11792, 20996: 11792, 26752: 3972, 27328: 26752, 21576: 27328, 28032: 21576,
           555: 1}


def test_desktop_s_server_is_found_while_its_window_is_connected():
    netstat = """
  TCP    127.0.0.1:63014        127.0.0.1:63558        ESTABLISHED     20996
  TCP    127.0.0.1:63558        127.0.0.1:0            LISTENING       0
  TCP    127.0.0.1:63558        127.0.0.1:63014        ESTABLISHED     3972
  TCP    127.0.0.1:63558        127.0.0.1:54541        TIME_WAIT       0
  TCP    127.0.0.1:9222         0.0.0.0:0              LISTENING       11792
  TCP    127.0.0.1:7000         0.0.0.0:0              LISTENING       555
"""
    urls = desktop.candidate_urls(28032, parents=PARENTS, netstat=netstat)
    # the server port first; client ends after it, for the health check to rule out; 7000 never
    assert urls[0] == "http://127.0.0.1:63558" and "http://127.0.0.1:7000" not in urls


def test_desktop_s_server_is_found_with_no_listening_entry_at_all():
    # what broke the first live run, from the runner (one level deeper than the MCP server):
    # netstat at 21:2x on 4 Nov listed no listener for 63558, only connections to it
    netstat = """
  TCP    127.0.0.1:50564        127.0.0.1:63558        ESTABLISHED     20996
  TCP    127.0.0.1:53311        127.0.0.1:63558        CLOSE_WAIT      20996
  TCP    127.0.0.1:63558        127.0.0.1:50564        ESTABLISHED     3972
  TCP    127.0.0.1:63558        127.0.0.1:53311        FIN_WAIT_2      3972
  TCP    127.0.0.1:63558        127.0.0.1:57105        FIN_WAIT_1      0
"""
    urls = desktop.candidate_urls(28032, parents=PARENTS, netstat=netstat)
    assert urls[0] == "http://127.0.0.1:63558"


def test_a_server_port_is_one_several_connections_use():
    netstat = """
  TCP    127.0.0.1:63014        127.0.0.1:63558        ESTABLISHED     20996
  TCP    127.0.0.1:63558        127.0.0.1:0            LISTENING       0
  TCP    127.0.0.1:63558        127.0.0.1:63014        ESTABLISHED     3972
"""
    ports, servers = desktop.loopback_ports(netstat)
    assert servers == {63558} and ports == {20996: [63014, 63558], 3972: [63558, 63014]}


# --- running a step ---------------------------------------------------------------------------

def test_a_step_runs_as_a_desktop_session_in_the_worktree(fake, tmp_path):
    fake.steps = [Step(1000, tool=("read", "src/a.py")), Step(1500, tool=("write", "tests/test_gtu_a.py")),
                  Step(500, text="created tests/test_gtu_a.py")]
    seen = []
    result = agent(fake, model="bharatcode/qwen-3.8-27b").run(tmp_path, "Write tests\nfor a.py", on_step=seen.append)
    assert result.ok and result.exit_code == 0
    assert result.tokens == 3000 and [s.total_tokens for s in seen] == [1000, 1500, 500]
    assert result.tools == ["read src/a.py", "write tests/test_gtu_a.py"]
    assert result.text == "created tests/test_gtu_a.py"
    (session,) = fake.sessions.values()
    assert session["directory"] == str(tmp_path) and session["title"] == "spare-gpu: Write tests"
    assert session["prompt"]["model"] == {"providerID": "bharatcode", "modelID": "qwen-3.8-27b"}
    assert session["prompt"]["parts"] == [{"type": "text", "text": "Write tests\nfor a.py"}]


def test_it_waits_for_the_session_to_go_idle_not_just_one_finished_step(fake, tmp_path):
    fake.steps = [Step(100), Step(100), Step(100, text="done")]
    result = agent(fake).run(tmp_path, "x")
    assert len(result.steps) == 3 and result.text == "done"


def test_the_permission_file_is_in_place_while_it_runs_and_removed_after(fake, tmp_path):
    inner = fake.transport()
    configs = []

    class Spy(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.path.endswith("/prompt_async"):
                configs.append(json.loads((tmp_path / "opencode.json").read_text()))
            return inner.handle_request(request)

    DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=Spy(), sleep=lambda s: None).run(
        tmp_path, "x", commands=("python -m pytest",))
    assert configs[0]["permission"]["bash"] == {"*": "deny", "python -m pytest*": "allow"}
    assert not (tmp_path / "opencode.json").exists()


def test_the_budget_aborts_the_session(fake, tmp_path):
    fake.steps = [Step(5000) for _ in range(10)]
    result = agent(fake).run(tmp_path, "x", token_budget=12000)
    assert result.stopped == "budget" and not result.ok and result.tokens == 15000
    assert next(iter(fake.sessions.values()))["aborted"]


def test_gtu_stop_aborts_the_session(fake, tmp_path):
    fake.steps = [Step(100) for _ in range(5)]
    result = agent(fake).run(tmp_path, "x", should_stop=lambda: True)
    assert result.stopped == "stop" and next(iter(fake.sessions.values()))["aborted"]


def test_timeout_aborts_the_session(fake, tmp_path):
    fake.steps = [Step(100) for _ in range(5)]
    ticks = iter(range(0, 10_000, 400))
    result = agent(fake, timeout=900, clock=lambda: next(ticks)).run(tmp_path, "x")
    assert result.stopped == "timeout" and next(iter(fake.sessions.values()))["aborted"]


def test_an_error_on_the_last_message_fails_the_step(fake, tmp_path):
    fake.steps = [Step(100)]
    fake.error = "Model overloaded"
    result = agent(fake).run(tmp_path, "x")
    assert not result.ok and result.error == "Model overloaded"


def test_a_session_left_idle_with_an_unfinished_reply_fails_the_step_instead_of_hanging(fake, tmp_path):
    fake.steps = [Step(100), Step(100)]
    fake.stalled = True
    polls = []
    result = DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=fake.transport(),
                          sleep=polls.append).run(tmp_path, "x")
    assert not result.ok and "without finishing its reply" in result.error
    assert len(polls) < 30 and next(iter(fake.sessions.values()))["aborted"]


def test_a_reply_still_being_written_is_waited_for(fake, tmp_path):
    fake.steps = [Step(100) for _ in range(desktop.STALL_POLLS + 5)] + [Step(100, text="done")]
    result = agent(fake).run(tmp_path, "x")
    assert result.ok and result.text == "done"


def wired_ask(fake, git_repo, tmp_path, monkeypatch):
    """spare_ask with Desktop and the wait figures faked: DeepSeek queued, Qwen idle."""
    from test_routing_jobs import wait_figures

    from gpu_token_utilization import waits
    from gpu_token_utilization.mcp_server import ASK_DIR_ENV, SpareTools

    waits._cache.clear()
    monkeypatch.setenv(ASK_DIR_ENV, str(tmp_path / "ask"))
    monkeypatch.setattr(desktop, "discover", lambda **kw: Connection(URL, "opencode", PASSWORD))
    real = desktop.DesktopAgent
    monkeypatch.setattr(desktop, "DesktopAgent",
                        lambda conn, **kw: real(conn, transport=fake.transport(), sleep=lambda s: None, **kw))
    return SpareTools(git_repo, launch=lambda repo: 4242, http=wait_figures)


def test_a_question_goes_to_the_faster_model_and_the_answer_comes_back(fake, git_repo, tmp_path, monkeypatch):
    fake.steps = [Step(300, text="The newest is the M5.")]
    text = wired_ask(fake, git_repo, tmp_path, monkeypatch).ask("what is the latest mac chip?",
                                                                 your_model="deepseek-v4.1-flash")
    assert text.startswith("The newest is the M5.")
    assert "Answered by Qwen 3.8 27B: saves about 2m 05s of waiting over DeepSeek V4.1 Flash" in text
    (session,) = fake.sessions.values()
    assert session["prompt"]["model"]["modelID"] == "qwen-3.8-27b"
    assert session["directory"] == str(tmp_path / "ask")  # never the user's repo
    assert {"permission": "edit", "pattern": "*", "action": "deny"} in session["permission"]
    assert not (git_repo / "opencode.json").exists()


def test_an_empty_question_asks_nothing(fake, git_repo, tmp_path, monkeypatch):
    assert "Nothing to ask" in wired_ask(fake, git_repo, tmp_path, monkeypatch).ask("  ")
    assert not fake.sessions


def test_ask_is_a_session_that_may_not_edit_or_run_anything(fake, tmp_path):
    inner = fake.transport()
    configs = []

    class Spy(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.path.endswith("/prompt_async"):
                configs.append(json.loads((tmp_path / "opencode.json").read_text()))
            return inner.handle_request(request)

    fake.steps = [Step(800, text="## Likely bugs\nNothing found.")]
    result = DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=Spy(), sleep=lambda s: None).ask(
        tmp_path, [{"role": "system", "content": "You review code."}, {"role": "user", "content": "Review a.py"}])
    assert result.ok and result.text == "## Likely bugs\nNothing found." and result.tokens == 800
    assert configs[0]["permission"]["edit"] == "deny" and configs[0]["permission"]["bash"] == "deny"
    prompt = next(iter(fake.sessions.values()))["prompt"]["parts"][0]["text"]
    assert prompt.startswith("You review code.\n\nReview a.py") and "Don't use any tools" in prompt


# --- inside Desktop, no API key is needed -------------------------------------------------------

def test_a_review_job_runs_as_a_desktop_session_without_a_client(fake, tmp_path, store):
    from datetime import time

    from gpu_token_utilization.fakeapi import FakeBharatCode
    from gpu_token_utilization.gate import Gate, GateConfig
    from gpu_token_utilization.scheduler import Scheduler

    api = FakeBharatCode(api_key="unused")  # only its live stats are read, never with a key
    gate = Gate(store, GateConfig(window=(time(0, 0), time(0, 0))), http=httpx.Client(transport=api.transport()),
                now=api.clock.utcnow)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f(x):\n    return 1 / x\n")
    job = store.add_job("review", "a.py", str(repo))
    fake.steps = [Step(900, text="## Likely bugs\n- `a.py:2` divides by zero when x is 0.")]
    done = Scheduler(store, gate, None, agent=agent(fake), isolate=False, ignore_gate=True).run()
    assert done == 1 and store.get_job(job.id).status == "done"
    report = (repo / f"gtu-review-{job.id}.md").read_text()
    assert "divides by zero" in report
    assert store.request_summary(job.id)["total_tokens"] == 900  # counted against the budget like any step


def delegating_tools(git_repo, launched):
    """spare_delegate with the wait figures faked and the runner launch recorded."""
    from test_routing_jobs import wait_figures

    from gpu_token_utilization import waits
    from gpu_token_utilization.mcp_server import SpareTools

    waits._cache.clear()
    return SpareTools(git_repo, launch=lambda repo: launched.append(repo) or 4242, http=wait_figures)


def test_delegating_inside_desktop_starts_the_runner_without_an_api_key(git_repo, monkeypatch):
    monkeypatch.setattr(desktop, "discover", lambda **kw: Connection(URL, "opencode", PASSWORD))
    monkeypatch.delenv(desktop.URL_ENV, raising=False)
    launched = []
    text = delegating_tools(git_repo, launched).delegate("review", "src")
    assert launched and "BharatCode Desktop session" in text and "API key" not in text
    assert desktop.os.environ[desktop.URL_ENV] == URL  # handed on to the detached runner


def test_delegating_outside_desktop_still_needs_a_key(git_repo):
    launched = []
    text = delegating_tools(git_repo, launched).delegate("review", "src")
    assert not launched and "no BharatCode API key" in text


def test_gtu_start_inside_desktop_needs_no_api_key(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from gpu_token_utilization.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setattr(desktop, "discover", lambda: Connection(URL, "opencode", PASSWORD))
    result = CliRunner().invoke(app, ["start"])
    assert result.exit_code == 0, result.output
    assert "no API key needed" in result.output and "Queue empty" in result.output


def test_the_session_carries_the_jobs_rules_and_hides_spare_gpus_own_tools(fake, tmp_path):
    agent(fake).run(tmp_path, "x", commands=("python -m pytest",))
    rules = fake.sessions["ses_1"]["permission"]
    assert {"permission": "bash", "pattern": "*", "action": "deny"} in rules
    assert {"permission": "bash", "pattern": "python -m pytest*", "action": "allow"} in rules
    assert {"permission": "skill", "pattern": "*", "action": "deny"} in rules
    assert rules[-1] == {"permission": "spare-gpu_*", "pattern": "*", "action": "deny"}  # last match wins


class Flaky(httpx.BaseTransport):
    """Drops the first `drops` progress checks, like the WinError 10054 seen on 4 Nov."""

    def __init__(self, inner, drops):
        self.inner, self.drops = inner, drops

    def handle_request(self, request):
        if request.method == "GET" and request.url.path.endswith("/message") and self.drops:
            self.drops -= 1
            raise httpx.ReadError("An existing connection was forcibly closed by the remote host", request=request)
        return self.inner.handle_request(request)


def test_a_dropped_connection_is_retried_not_a_failed_job(fake, tmp_path):
    fake.steps = [Step(100), Step(100, text="done")]
    waited = []
    result = DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=Flaky(fake.transport(), 2),
                          sleep=waited.append).run(tmp_path, "x")
    assert result.ok and result.text == "done" and len(result.steps) == 2
    assert waited[:3] == [2.0, 4.0, 8.0]  # backs off while the connection is down


def test_a_server_that_stays_unreachable_fails_the_job_and_stops_its_session(fake, tmp_path):
    result = DesktopAgent(Connection(URL, "opencode", PASSWORD), transport=Flaky(fake.transport(), 99),
                          sleep=lambda s: None).run(tmp_path, "x")
    assert not result.ok and "forcibly closed" in result.error
    assert fake.sessions["ses_1"]["aborted"]  # no orphan session left spending tokens


def test_desktops_default_model_is_read_from_its_provider_list():
    def handle(request):
        return httpx.Response(200, json={"default": {"bharatcode": "qwen-3.8-27b"}, "all": []})
    conn = Connection(URL, "opencode", PASSWORD)
    assert desktop.default_model(conn, transport=httpx.MockTransport(handle)) == "qwen-3.8-27b"
    broken = httpx.MockTransport(lambda r: httpx.Response(500, text="nope"))
    assert desktop.default_model(conn, transport=broken) is None


def test_an_unreachable_server_is_a_clear_failure(tmp_path):
    result = DesktopAgent(Connection("http://127.0.0.1:1", "opencode", "x"), sleep=lambda s: None).run(tmp_path, "x")
    assert not result.ok and "BharatCode Desktop's server" in result.error
    assert not (tmp_path / "opencode.json").exists()
