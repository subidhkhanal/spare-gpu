"""`gtu integrate`, the one command you type: registering the MCP server in BharatCode's config without breaking it."""

import json
import re

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config
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


# --- gtu integrate ---------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "BharatCode" / "Config"
    folder.mkdir(parents=True)
    (folder / "opencode.jsonc").write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}', encoding="utf-8")
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(folder))
    return folder


def test_integrate_says_what_it_found_changed_and_how_to_undo(config_dir):
    result = cli.invoke(app, ["integrate"])
    out = result.output
    assert result.exit_code == 0, out
    assert f"Config file: {config_dir / 'opencode.jsonc'} (from GTU_BHARATCODE_CONFIG_DIR)" in out
    assert 'Added the "spare-gpu" entry under "mcp"' in out and "mcp" in out
    assert "opencode.jsonc.spare-gpu-backup" in out and "To undo: gtu integrate --uninstall" in out
    backup = config_dir / "opencode.jsonc.spare-gpu-backup"
    assert "mcp" not in backup.read_text(encoding="utf-8")  # the backup is the file as it was


def test_integrate_is_idempotent(config_dir):
    cli.invoke(app, ["integrate"])
    config, backup = config_dir / "opencode.jsonc", config_dir / "opencode.jsonc.spare-gpu-backup"
    before, backup_before = config.read_text(encoding="utf-8"), backup.read_text(encoding="utf-8")
    for _ in range(2):
        again = cli.invoke(app, ["integrate"])
        assert again.exit_code == 0 and "already registered there; nothing changed" in again.output
        assert "To undo" not in again.output
    assert config.read_text(encoding="utf-8") == before
    assert backup.read_text(encoding="utf-8") == backup_before  # the original backup isn't overwritten


def test_integrate_uninstall(config_dir):
    cli.invoke(app, ["integrate"])
    result = cli.invoke(app, ["integrate", "--uninstall"])
    assert result.exit_code == 0 and "Removed spare-gpu from" in result.output and "your jobs are kept" in result.output
    assert entry_in(config_dir / "opencode.jsonc") is None
    again = cli.invoke(app, ["integrate", "--uninstall"])
    assert again.exit_code == 0 and "wasn't registered" in again.output


def test_integrate_prints_the_snippet_when_it_cannot_edit_safely(config_dir):
    config = config_dir / "opencode.jsonc"
    config.write_text('{"theme": "dark",, }', encoding="utf-8")
    result = cli.invoke(app, ["integrate"])
    assert result.exit_code == 1
    assert "Couldn't change it automatically" in result.output and f"Add this to {config} by hand" in result.output
    assert '"spare-gpu": {' in result.output and '"type": "local"' in result.output
    assert config.read_text(encoding="utf-8") == '{"theme": "dark",, }'


def test_integrate_registers_in_every_installed_desktop_channel(config_dir):
    dev = config_dir / "BharatCode Dev"  # under the override, where tests keep the other channels
    dev.mkdir()
    result = cli.invoke(app, ["integrate"])
    assert result.exit_code == 0, result.output
    assert "BharatCode stable:" in result.output and "BharatCode dev:" in result.output
    assert "BharatCode Desktop's dev channel" in result.output and "Restart BharatCode Desktop" in result.output
    assert entry_in(config_dir / "opencode.jsonc") is not None and entry_in(dev / "opencode.jsonc") is not None
    assert "BharatCode beta:" not in result.output  # not installed here


def test_integrate_one_channel_and_uninstall_it(config_dir):
    dev = config_dir / "BharatCode Dev"
    dev.mkdir()
    assert cli.invoke(app, ["integrate", "--channel", "dev"]).exit_code == 0
    assert entry_in(dev / "opencode.jsonc") is not None and entry_in(config_dir / "opencode.jsonc") is None
    removed = cli.invoke(app, ["integrate", "--uninstall", "--channel", "dev"])
    assert removed.exit_code == 0 and entry_in(dev / "opencode.jsonc") is None
    assert cli.invoke(app, ["integrate", "--channel", "nightly"]).exit_code == 1


def test_integrate_adds_the_auto_command_and_uninstall_removes_it(config_dir):
    command = config_dir / "command" / "auto.md"
    result = cli.invoke(app, ["integrate"])
    assert result.exit_code == 0, result.output
    assert "Added the /auto command" in result.output and command.is_file()
    text = command.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "description:" in text and "$ARGUMENTS" in text and "spare_auto" in text
    agent = config_dir / "agent" / "spare-auto.md"
    assert "agent: spare-auto" in text and agent.is_file()  # the small handoff agent comes with it
    assert "The /auto command is already there" in cli.invoke(app, ["integrate"]).output
    removed = cli.invoke(app, ["integrate", "--uninstall"])
    assert "Removed the /auto command" in removed.output and not command.exists() and not agent.exists()


def test_integrate_never_overwrites_or_removes_the_users_own_auto_command(config_dir):
    command = config_dir / "command" / "auto.md"
    command.parent.mkdir()
    command.write_text("---\ndescription: mine\n---\nmy own /auto\n", encoding="utf-8")
    result = cli.invoke(app, ["integrate"])
    assert "You already have a /auto command" in result.output
    cli.invoke(app, ["integrate", "--uninstall"])
    assert command.read_text(encoding="utf-8") == "---\ndescription: mine\n---\nmy own /auto\n"


def test_an_older_auto_command_from_spare_gpu_is_updated(config_dir):
    command = config_dir / "command" / "auto.md"
    command.parent.mkdir()
    command.write_text("---\n# Installed by spare-gpu (old)\n---\nold text\n", encoding="utf-8")
    assert "Updated the /auto command" in cli.invoke(app, ["integrate"]).output
    assert "$ARGUMENTS" in command.read_text(encoding="utf-8")


def test_the_auto_command_is_installed_in_each_desktop_channel(config_dir):
    dev = config_dir / "BharatCode Dev"
    dev.mkdir()
    assert cli.invoke(app, ["integrate"]).exit_code == 0
    assert (dev / "command" / "auto.md").is_file() and (config_dir / "command" / "auto.md").is_file()


def test_integrate_uses_desktops_config_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    result = cli.invoke(app, ["integrate"])
    assert result.exit_code == 0, result.output
    assert "BharatCode Desktop's config folder" in result.output
    assert entry_in(tmp_path / "BharatCode" / "Config" / "opencode.jsonc") is not None


def test_the_help_lists_only_integrate_and_the_hidden_mcp_still_works():
    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    # a command's name starts its row; its help's wrapped lines are indented under the description
    listed = re.findall(r"^\W (\w[\w-]*) ", result.output.split("Commands")[1], re.M)
    assert listed == ["integrate"], result.output
    for gone in ("setup", "doctor", "pack-check", "init", "start", "list", "apply", "undo", "cost", "prompts"):
        assert cli.invoke(app, [gone]).exit_code == 2  # no such command
    mcp = cli.invoke(app, ["mcp", "--help"])
    assert mcp.exit_code == 0 and "--dir" in mcp.output and "--pack" in mcp.output
