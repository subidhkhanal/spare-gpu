"""`gtu setup`: registering the MCP server in BharatCode's config without breaking it, and the
background runner that `spare_add_job` / `spare_start` launch."""

import json
import subprocess
import sys
import textwrap
import time

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, runner, runstate
from gpu_token_utilization.bc_config import ConfigError, parse_jsonc, register, unregister
from gpu_token_utilization.cli import app

cli = CliRunner()
COMMAND = ["C:\\Users\\me\\.local\\bin\\gtu.exe", "mcp"]
ENTRY = {"type": "local", "command": COMMAND, "enabled": True}


def entry_in(path):
    return parse_jsonc(path.read_text(encoding="utf-8")).get("mcp", {}).get("spare-gpu")


# --- editing BharatCode's config -------------------------------------------------------------

def test_jsonc_with_comments_and_trailing_commas():
    text = '{\n  // a comment\n  "a": "http://x//y", /* block */\n  "b": [1, 2,],\n}\n'
    assert parse_jsonc(text) == {"a": "http://x//y", "b": [1, 2]}


def test_register_into_a_missing_file(tmp_path):
    path = tmp_path / "opencode.jsonc"
    assert register(path, COMMAND) == "added"
    assert entry_in(path) == ENTRY and json.loads(path.read_text())["$schema"]


def test_register_into_bharatcodes_default_file_keeps_it_intact(tmp_path):
    # exactly the file a fresh BharatCode install has
    path = tmp_path / "opencode.jsonc"
    path.write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}', encoding="utf-8")
    assert register(path, COMMAND) == "added"
    config = parse_jsonc(path.read_text(encoding="utf-8"))
    assert config["$schema"] == "https://opencode.ai/config.json" and config["mcp"]["spare-gpu"] == ENTRY
    assert (tmp_path / "opencode.jsonc.spare-gpu-backup").exists()


def test_register_keeps_the_users_comments_and_other_servers(tmp_path):
    path = tmp_path / "opencode.jsonc"
    original = ('{\n  // my settings\n  "theme": "dark",\n  "mcp": {\n    // my own server\n'
                '    "other": {"type": "local", "command": ["x"]},\n  },\n}\n')
    path.write_text(original, encoding="utf-8")
    assert register(path, COMMAND) == "added"
    text = path.read_text(encoding="utf-8")
    assert "// my settings" in text and "// my own server" in text
    config = parse_jsonc(text)
    assert config["theme"] == "dark" and set(config["mcp"]) == {"other", "spare-gpu"}


def test_register_into_an_empty_mcp_object(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text('{"mcp": {}}', encoding="utf-8")
    register(path, COMMAND)
    assert entry_in(path) == ENTRY


def test_register_again_is_unchanged_and_a_new_path_updates(tmp_path):
    path = tmp_path / "opencode.jsonc"
    register(path, COMMAND)
    before = path.read_text(encoding="utf-8")
    assert register(path, COMMAND) == "unchanged" and path.read_text(encoding="utf-8") == before
    assert register(path, ["D:\\gtu.exe", "mcp"]) == "updated"
    assert entry_in(path)["command"] == ["D:\\gtu.exe", "mcp"]
    assert entry_in(tmp_path / "opencode.jsonc.spare-gpu-backup") == ENTRY


def test_a_broken_config_is_never_touched(tmp_path):
    path = tmp_path / "opencode.jsonc"
    path.write_text('{"theme": "dark",, }', encoding="utf-8")
    with pytest.raises(ConfigError, match="isn't valid JSON"):
        register(path, COMMAND)
    assert path.read_text(encoding="utf-8") == '{"theme": "dark",, }'


def test_unregister(tmp_path):
    path = tmp_path / "opencode.jsonc"
    path.write_text('{"theme": "dark"}', encoding="utf-8")
    register(path, COMMAND)
    assert unregister(path) is True and parse_jsonc(path.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert unregister(path) is False


def test_existing_config_file_names_are_found_and_bharatcode_json_is_not_used(tmp_path):
    (tmp_path / "bharatcode.json").write_text("{}")
    assert bc_config.config_file(tmp_path).name == "opencode.jsonc"
    (tmp_path / "opencode.json").write_text("{}")
    assert bc_config.config_file(tmp_path).name == "opencode.json"


# --- gtu setup -------------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "BharatCode" / "Config"
    folder.mkdir(parents=True)
    (folder / "opencode.jsonc").write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}', encoding="utf-8")
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(folder))
    return folder


def test_setup_with_a_key_registers_and_confirms_the_connection(config_dir, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    result = cli.invoke(app, ["setup"])
    out = result.output
    assert result.exit_code == 0, out
    assert "1/3" in out and "Found in the BHARATCODE_API_KEY environment variable" in out
    assert "Installed (0.0.0-fake) and signed in" in out
    assert "Registered in" in out and "BharatCode reports: spare-gpu connected" in out
    assert "queue tests for src/utils tonight" in out
    assert entry_in(config_dir / "opencode.jsonc")["command"][-1] == "mcp"
    again = cli.invoke(app, ["setup"])
    assert again.exit_code == 0 and "Already registered" in again.output


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_setup_without_a_key_asks_for_it_once(config_dir):
    from gpu_token_utilization import keystore

    result = cli.invoke(app, ["setup", "--no-check"], input="bc_live_typed_in_setup\n")
    assert result.exit_code == 0, result.output
    assert keystore.load() == "bc_live_typed_in_setup" and "Registered in" in result.output
    assert "bc_live_typed_in_setup" not in result.output


def test_setup_without_bharatcodes_cli_still_registers(config_dir, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    monkeypatch.delenv("GTU_BHARATCODE_CMD")
    monkeypatch.setattr("gpu_token_utilization.agent_cli.shutil.which", lambda name: None)
    result = cli.invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    assert "Not installed yet" in result.output and "npm install -g bharatcode" in result.output
    assert entry_in(config_dir / "opencode.jsonc") is not None


def test_setup_remove(config_dir, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    cli.invoke(app, ["setup"])
    result = cli.invoke(app, ["setup", "--remove"])
    assert result.exit_code == 0 and "Removed spare-gpu" in result.output
    assert entry_in(config_dir / "opencode.jsonc") is None


# --- the background runner -------------------------------------------------------------------

# Stands in for `gtu start`: registers itself like the real runner, then waits to be killed.
FAKE_RUNNER = textwrap.dedent("""
    import time
    from pathlib import Path
    from gpu_token_utilization import runstate
    runstate.write_run(Path(".gtu"))
    print("fake runner up", flush=True)
    time.sleep(120)
""")


def test_launch_returns_once_the_runner_has_registered(tmp_path):
    pid = runner.launch(tmp_path, [sys.executable, "-c", FAKE_RUNNER])
    try:
        assert runstate.running_pid(tmp_path / ".gtu") == pid
    finally:
        runstate.kill_tree(pid) or subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    assert "fake runner up" in (tmp_path / ".gtu" / "runner.log").read_text(encoding="utf-8")


def test_launch_reports_a_runner_that_fails_at_once(tmp_path):
    with pytest.raises(runner.RunnerError, match="stopped straight away"):
        runner.launch(tmp_path, [sys.executable, "-c", "print('no key, giving up'); raise SystemExit(1)"])


@pytest.mark.skipif(sys.platform != "win32", reason="detaching is Windows-specific here")
def test_the_runner_outlives_the_process_that_started_it(tmp_path):
    # BharatCode starts the MCP server; the MCP server starts the runner; BharatCode closes.
    parent = (f"import sys; from gpu_token_utilization import runner; "
              f"print(runner.launch(sys.argv[1], [sys.executable, '-c', {FAKE_RUNNER!r}]))")
    proc = subprocess.run([sys.executable, "-c", parent, str(tmp_path)], capture_output=True, text=True, timeout=60)
    pid = int(proc.stdout.strip())
    time.sleep(1)  # the parent is gone now
    try:
        assert runstate.alive(pid) and runstate.running_pid(tmp_path / ".gtu") == pid
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
