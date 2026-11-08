"""`gtu doctor`: a plain install passes; each problem is named with its fix; the MCP server is really
started; `--live` measures one tiny run as a (fake) BharatCode Desktop session and fills the
manual-check rows it can decide. Fakes only: no real BharatCode, no network."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, desktop, doctor, pack, runstate
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakedesktop import URL, FakeDesktop, Step
from gpu_token_utilization.store import Store

from test_pack_install import COMMAND, LEAD, SCOUT

cli = CliRunner()
REPO_RESEARCH = Path(__file__).resolve().parents[1] / "research"


@pytest.fixture(autouse=True)
def research(tmp_path, monkeypatch):
    """A copy of the manual-check tables, so a test never writes into the repo's own."""
    folder = tmp_path / "research"
    for name in ("pack-install", "safety-fixes"):
        (folder / name).mkdir(parents=True)
        shutil.copy(REPO_RESEARCH / name / "manual-check.md", folder / name / "manual-check.md")
    monkeypatch.setattr(doctor, "RESEARCH", folder)
    return folder


@pytest.fixture(autouse=True)
def known_models(monkeypatch):
    monkeypatch.setenv("GTU_MODELS", "deepseek-v4.1-flash,qwen-3.8-27b")


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "BharatCode" / "Config"
    folder.mkdir(parents=True)
    (folder / "opencode.jsonc").write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}', encoding="utf-8")
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(folder))
    return folder


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    source = tmp_path / "pack"
    (source / "commands").mkdir(parents=True)
    (source / "agents").mkdir()
    (source / "commands" / "deep-research.md").write_text(COMMAND, encoding="utf-8")
    (source / "agents" / "research-lead.md").write_text(LEAD, encoding="utf-8")
    (source / "agents" / "research-scout.md").write_text(SCOUT, encoding="utf-8")
    monkeypatch.setattr(pack, "PACK_DIR", source)
    for fn in (pack.markdown_files, pack.helpers, pack.config, pack.install_files):
        monkeypatch.setattr(fn, "__defaults__", (source,))
    return source


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    return git_repo


def out(result):
    return result.output


# --- static ------------------------------------------------------------------------------------

def test_a_plain_install_passes_and_the_server_is_really_started(config_dir, project):
    assert cli.invoke(app, ["integrate"]).exit_code == 0
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    for line in ("PASS: spare-gpu ", "PASS: git: git version", "PASS: BharatCode's config folder:", "PASS: spare-gpu is registered in opencode.jsonc and its command exists",
                 "PASS: the /auto command is installed", "PASS: the command pack isn't installed (optional)",
                 "PASS: spare-gpu's MCP server starts and offers 5 tools", "PASS: project:",
                 "PASS: no jobs have run here yet", "PASS: test command for jobs:",
                 "PASS: no API key set, which is fine inside Desktop", "PASS: job types that may move to the faster model: none"):
        assert line in text, line
    assert text.splitlines()[-2] == "Result: PASS" and "Add --live" in text.splitlines()[-1]
    assert not any(line.startswith(("FAIL", "WARN")) for line in text.splitlines())


def test_not_registered_and_a_dead_command_are_failures_with_the_fix(config_dir, project):
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL: spare-gpu isn't registered as an MCP server in opencode.jsonc; run `gtu integrate`" in out(result)
    assert "WARN: the /auto command isn't installed; run `gtu integrate`" in out(result)
    bc_config.register(config_dir / "opencode.jsonc", ["C:\\nowhere\\gtu.exe", "mcp"])
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL: the registered command C:\\nowhere\\gtu.exe doesn't exist, so Desktop can't start spare-gpu; run `gtu integrate`" in out(result)
    assert "MCP server" not in out(result).split("FAIL: the registered")[1].split("PASS: project")[0]  # not started


def test_a_server_that_fails_to_start_is_reported_with_its_error(config_dir, project):
    bc_config.register(config_dir / "opencode.jsonc", [sys.executable, "-c", "raise SystemExit('boom')"])
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL: spare-gpu's MCP server didn't start from `" in out(result)
    assert "Run that command in a terminal to see its error" in out(result)


def test_missing_git_is_a_failure_and_our_own_server_still_starts(config_dir, project, monkeypatch):
    cli.invoke(app, ["integrate"])
    real_which = shutil.which
    monkeypatch.setattr(doctor.shutil, "which", lambda name, *a, **k: None if name == "git" else real_which(name, *a, **k))
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL: git isn't installed or isn't on PATH; jobs copy your project with git" in out(result)
    assert "PASS: spare-gpu's MCP server starts" in out(result)  # our own server doesn't need git


def test_project_state_problems_each_get_a_line(config_dir, project):
    cli.invoke(app, ["integrate"])
    folder = project / ".gtu"
    with Store(folder / "gtu.db") as store:
        running = store.add_job("review", "src/calc.py", str(project))
        store.set_status(running.id, "running")
        paused = store.add_job("docs", "src", str(project))
        store.set_status(paused.id, "paused")
        done = store.add_job("tests", "src", str(project))
        store.set_status(done.id, "running")
        store.set_status(done.id, "done")
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (folder / runstate.RUN_FILE).write_text(json.dumps({"pid": int(gone.stdout)}))
    runstate.request_stop(folder)
    (folder / "worktrees" / done.id).mkdir(parents=True)
    (folder / "worktrees" / running.id).mkdir()  # the running job's copy is legitimate
    result = cli.invoke(app, ["doctor"])
    text = out(result)
    assert result.exit_code == 0, text  # warnings, not failures
    assert "PASS: jobs here: 1 done, 1 paused, 1 running" in text
    assert f"WARN: a run file is left from process {int(gone.stdout)}, which is gone" in text
    assert "WARN: a stop request is pending with nothing running" in text
    assert f"WARN: job(s) marked running with no runner: {running.id}; `gtu stop` pauses them" in text
    assert f"PASS: paused job(s) {paused.id}: `gtu start` resumes them" in text
    assert f"WARN: leftover job copies with no unfinished job: {done.id} in" in text and running.id not in text.split("leftover")[1].split("\n")[0]


def test_bad_job_settings_fail_and_an_api_key_is_named(config_dir, project, monkeypatch):
    cli.invoke(app, ["integrate"])
    monkeypatch.setenv("GTU_TEST_COMMAND", "rm -rf /")
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL: job settings: test command 'rm -rf /' is not in the allowlist" in out(result)
    assert "PASS: API key: found in the BHARATCODE_API_KEY environment variable (only needed outside Desktop)" in out(result)


def test_with_the_pack_the_pack_checks_are_included(config_dir, pack_dir, project):
    assert cli.invoke(app, ["integrate", "--pack"]).exit_code == 0
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, out(result)
    assert "PASS: pack: spare-gpu's MCP server starts with --pack" in out(result)
    assert "PASS: pack: installed components: split, research" in out(result)
    assert "PASS: spare-gpu's MCP server starts and offers 10 tools (the pack's: build_report, pack_cost, scholar_search, security_filter, verify_refs)" in out(result)
    (config_dir / "config.json").unlink()  # the pack's settings file gone, the --pack flag left behind
    result = cli.invoke(app, ["doctor"])
    assert "WARN: spare-gpu's MCP server starts with --pack although the pack's settings file is gone" in out(result)


def test_unknown_channel_is_refused(config_dir, project):
    assert cli.invoke(app, ["doctor", "--channel", "nightly"]).exit_code == 1


# --- live --------------------------------------------------------------------------------------

@pytest.fixture
def live_desktop(monkeypatch):
    """`gtu doctor --live` against a fake Desktop server; `make(tokens)` scripts its one-step reply
    (the first step's context is tokens - 10) and returns the fake, whose sessions it records."""
    fake = FakeDesktop()
    real = desktop.DesktopAgent
    monkeypatch.setattr(desktop, "discover", lambda **kw: desktop.Connection(URL, "opencode", fake.password))
    monkeypatch.setattr(desktop, "DesktopAgent",
                        lambda conn, **kw: real(conn, transport=fake.transport(), sleep=lambda s: None, **kw))

    def make(tokens=12_000, text="OK"):
        fake.steps = [Step(tokens, text=text)]
        return fake

    return make


def result_cell(research, name, row):
    for line in (research / name / "manual-check.md").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| {row} |"):
            return line.rstrip().rsplit(" | ", 1)[1].rstrip(" |")
    raise AssertionError(f"no row {row} in {name}")


def test_live_measures_a_desktop_session_and_fills_the_rows_it_can_decide(config_dir, pack_dir, project, live_desktop, research):
    assert cli.invoke(app, ["integrate", "--pack"]).exit_code == 0
    fake = live_desktop(tokens=12_000)
    result = cli.invoke(app, ["doctor", "--live"])
    text = out(result)
    assert result.exit_code == 0, text
    assert "PASS: live: a one-step request ran end to end on Qwen 3.8 27B (reply 'OK'); context sent before your words: 11,990 tokens, with the pack" in text
    assert "WARN: live: measured 11,990 tokens with the pack; run `gtu doctor --live` once more with the pack uninstalled" in text
    assert "PASS: a job's copy leaves dist/, build/ and node_modules/ out" in text
    assert "PASS: manual-check rows filled from these results: pack-install 2, pack-install 8, safety-fixes 5" in text
    assert "Still manual: pack-install 1, 3 to 7, 9 and 10" in text
    assert result_cell(research, "pack-install", 2).startswith("PASS (gtu doctor")
    assert result_cell(research, "pack-install", 8).startswith("PARTIAL") and "11,990 tokens per step with the pack on Qwen 3.8 27B" in result_cell(research, "pack-install", 8)
    assert result_cell(research, "safety-fixes", 5).startswith("PASS (gtu doctor --live")
    for row in (1, 3, 6, 7):
        assert result_cell(research, "pack-install", row) == ""  # rows the doctor can't decide stay empty
    runs = (research / "pack-install" / "overhead-runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(runs) == 1 and json.loads(runs[0])["pack_installed"] is True and json.loads(runs[0])["context_tokens"] == 11_990
    assert json.loads(runs[0])["model"] == "bharatcode/qwen-3.8-27b" and json.loads(runs[0])["desktop_version"] == "dev"
    (session,) = fake.sessions.values()
    assert session["prompt"]["model"] == {"providerID": "bharatcode", "modelID": "qwen-3.8-27b"}  # pinned
    assert "agent" not in session["prompt"] and session["permission"] is None  # Desktop's default agent, as a chat


def test_live_measures_the_overhead_once_both_states_have_run(config_dir, pack_dir, project, live_desktop, research):
    cli.invoke(app, ["integrate", "--pack"])
    live_desktop(tokens=12_000)
    assert cli.invoke(app, ["doctor", "--live"]).exit_code == 0
    assert cli.invoke(app, ["integrate", "--pack", "--uninstall"]).exit_code == 0
    live_desktop(tokens=11_000)
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, out(result)
    assert "PASS: live: the pack adds 1,000 tokens per step on Qwen 3.8 27B (10,990 without, measured " in out(result)
    assert "; 11,990 with, measured " in out(result) and "budget 1,500); written to research/pack-install/overhead.json" in out(result)
    recorded = json.loads((research / "pack-install" / "overhead.json").read_text(encoding="utf-8"))
    assert recorded["added"] == 1_000 and recorded["within_budget"] is True and recorded["variant"] == "empty-mcp"
    assert recorded["model"] == "bharatcode/qwen-3.8-27b" and recorded["desktop_version"] == "dev" and set(recorded["measured"]) == {"without_pack", "with_pack"}
    assert result_cell(research, "pack-install", 8).startswith("PASS (gtu doctor --live") and "added 1,000 tokens per step" in result_cell(research, "pack-install", 8)
    # over budget: the newest measurement of each state counts
    cli.invoke(app, ["integrate", "--pack"])
    live_desktop(tokens=13_000)
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 1 and "FAIL: live: the pack adds 2,000 tokens per step" in out(result)
    assert json.loads((research / "pack-install" / "overhead.json").read_text(encoding="utf-8"))["within_budget"] is False


def test_only_runs_on_the_same_model_and_desktop_version_are_compared(config_dir, pack_dir, project, live_desktop, research):
    runs = research / "pack-install" / "overhead-runs.jsonl"
    runs.write_text('{"date": "2026-11-01", "variant": "empty-mcp", "pack_installed": false, "context_tokens": 9000}\n'  # no model: an older record
                    '{"date": "2026-11-02", "variant": "empty-mcp", "pack_installed": false, "context_tokens": 9500, '
                    '"model": "bharatcode/qwen-3.8-27b", "desktop_version": "older"}\n'
                    'this line is not json\n', encoding="utf-8")
    cli.invoke(app, ["integrate", "--pack"])
    live_desktop(tokens=12_000)
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, out(result)
    assert "WARN: live: overhead-runs.jsonl line 3 isn't JSON and was skipped" in out(result)
    assert "WARN: live: measured 11,990 tokens with the pack; run `gtu doctor --live` once more" in out(result)
    assert not (research / "pack-install" / "overhead.json").exists()  # neither old record pairs with this one


def test_a_zero_token_step_is_not_a_measurement(config_dir, project, live_desktop, research):
    cli.invoke(app, ["integrate"])
    live_desktop(tokens=10)  # the fake reports input = tokens - 10
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 1 and "FAIL: live: the tiny request produced no step or reported no token usage" in out(result)
    assert not (research / "pack-install" / "overhead-runs.jsonl").exists()


def test_the_users_own_agents_and_settings_are_not_called_pack_leakage(config_dir, pack_dir, project, live_desktop, research):
    cli.invoke(app, ["integrate", "--pack"])
    user = config_dir / "opencode.jsonc"
    doc = bc_config.parse_jsonc(user.read_text(encoding="utf-8"))
    doc["agent"] = {"mine": {"prompt": "my own agent", "mode": "subagent"}}
    doc["permission"] = {"task": {"mine": "allow"}}
    user.write_text(json.dumps(doc), encoding="utf-8")
    live_desktop()
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, out(result)
    assert "carries the pack's settings" not in out(result) and result_cell(research, "pack-install", 2).startswith("PASS")
    doc["small_model"] = "bharatcode/qwen-3.8-27b"  # the pack's own setting copied into the user's file
    user.write_text(json.dumps(doc), encoding="utf-8")
    result = cli.invoke(app, ["doctor", "--live"])
    assert "WARN: live: your own config file opencode.jsonc carries the pack's settings (small_model)" in out(result)
    assert result_cell(research, "pack-install", 2).startswith("FAIL") and "small_model" in result_cell(research, "pack-install", 2)


def test_a_config_whose_mcp_is_not_an_object_is_a_failure_not_a_crash(config_dir, project):
    (config_dir / "opencode.jsonc").write_text(json.dumps({"mcp": ["spare-gpu"]}), encoding="utf-8")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1 and 'FAIL: "mcp" in opencode.jsonc isn\'t an object' in out(result)
    (config_dir / "opencode.jsonc").write_text(json.dumps({"mcp": {"spare-gpu": True}}), encoding="utf-8")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL: spare-gpu's MCP entry in opencode.jsonc isn't an object; run `gtu integrate`" in out(result)
    assert cli.invoke(app, ["pack-check"]).exit_code == 1  # the pack check survives it too


def test_the_project_checks_look_where_the_gtu_commands_look(config_dir, project, tmp_path, monkeypatch):
    cli.invoke(app, ["integrate"])
    elsewhere = tmp_path / "elsewhere" / "gtu.db"
    elsewhere.parent.mkdir()
    with Store(elsewhere) as store:
        store.add_job("review", "src", str(project))
    monkeypatch.setenv("GTU_DB", str(elsewhere))
    result = cli.invoke(app, ["doctor"])
    assert "PASS: jobs here: 1 queued" in out(result)
    monkeypatch.delenv("GTU_DB")
    sub = project / "src"
    monkeypatch.chdir(sub)
    with Store(project / ".gtu" / "gtu.db") as store:
        store.add_job("docs", "src", str(project))
    result = cli.invoke(app, ["doctor"])
    assert f"WARN: jobs live in {project / '.gtu'}, not under this folder; run the gtu commands from {project}" in out(result)
    assert "PASS: jobs here: 1 queued" in out(result)


def test_live_outside_desktop_says_so_and_no_write_keeps_the_tables(config_dir, project, live_desktop, research, monkeypatch):
    cli.invoke(app, ["integrate"])
    before = (research / "pack-install" / "manual-check.md").read_text(encoding="utf-8")
    found = desktop.discover  # the fake Desktop, from the fixture
    monkeypatch.setattr(desktop, "discover", lambda **kw: (_ for _ in ()).throw(desktop.DesktopNotFound("not started by Desktop")))
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))  # no git either
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 1
    assert "FAIL: live: the measurement runs as a BharatCode Desktop session, and Desktop wasn't found (not started by Desktop)" in out(result)
    assert "FAIL: a job's copy can't be made here: git isn't installed" in out(result)
    monkeypatch.setenv("PATH", path)
    monkeypatch.setattr(desktop, "discover", found)
    live_desktop()
    assert cli.invoke(app, ["doctor", "--live", "--no-write"]).exit_code == 0
    assert (research / "pack-install" / "manual-check.md").read_text(encoding="utf-8") == before


def test_outside_a_checkout_the_measurement_is_not_recorded(config_dir, project, live_desktop, tmp_path, monkeypatch):
    cli.invoke(app, ["integrate"])
    live_desktop()
    monkeypatch.setattr(doctor, "RESEARCH", tmp_path / "not-a-checkout")
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, out(result)
    assert "WARN: live: this isn't a checkout, so the measurement isn't recorded" in out(result)


# --- pieces ------------------------------------------------------------------------------------

def test_fill_manual_checks_replaces_only_the_result_cell_and_keeps_line_endings(research):
    path = research / "pack-install" / "manual-check.md"
    crlf = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    path.write_bytes(crlf.encode("utf-8"))
    written = doctor.fill_manual_checks(research, {("pack-install", 2): "PASS | with a pipe", ("pack-install", 10): "ten", ("nowhere", 1): "x"})
    assert written == ["pack-install 2", "pack-install 10"]
    text = path.read_bytes().decode("utf-8")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")  # every ending is still CRLF
    line = next(l for l in text.split("\r\n") if l.startswith("| 2 |"))
    assert line.endswith("| PASS / with a pipe |") and "Compare `opencode.jsonc` before and after." in line
    assert next(l for l in text.split("\r\n") if l.startswith("| 10 |")).endswith("| ten |")
    assert next(l for l in text.split("\r\n") if l.startswith("| 1 |")).endswith("| |")  # untouched rows unchanged
