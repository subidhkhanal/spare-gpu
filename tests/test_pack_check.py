"""`gtu pack-check`: a fresh install passes; a missing --pack flag, a deleted file, a stale settings
file, an edited file and a conflicting user setting are each reported with the fix."""

import json

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, pack
from gpu_token_utilization.cli import app
from gpu_token_utilization.pack import check

from test_pack_install import COMMAND, LEAD, SCOUT

cli = CliRunner()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "BharatCode" / "Config"
    folder.mkdir(parents=True)
    (folder / "opencode.jsonc").write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}', encoding="utf-8")
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(folder))
    return folder


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    """The same sample pack as test_pack_install: one research command, a lead and a scout."""
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


@pytest.fixture(autouse=True)
def known_models(monkeypatch):
    monkeypatch.setenv("GTU_MODELS", "deepseek-v4.1-flash,qwen-3.8-27b")


def lines(result):
    return result.output.splitlines()


def test_a_fresh_install_passes(config_dir, pack_dir):
    assert cli.invoke(app, ["integrate", "--pack"]).exit_code == 0
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 0, result.output
    assert "PASS: spare-gpu's MCP server starts with --pack" in result.output
    assert "PASS: installed components: split, research" in result.output
    assert "PASS: files: every command and agent of the installed components is there" in result.output
    assert "PASS: models in the files: heavy bharatcode/deepseek-v4.1-flash, standard bharatcode/qwen-3.8-27b" in result.output
    assert "PASS: the settings file matches the installed components" in result.output
    assert "switchable roles on the heavy model until measured: research-checker, review-verifier, security-verifier" in result.output
    assert lines(result)[-1] == "Result: PASS" and not any(l.startswith(("FAIL", "WARN")) for l in lines(result))


def test_without_the_pack_at_all(config_dir):
    cli.invoke(app, ["integrate"])
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 1
    assert "FAIL: spare-gpu's MCP server starts without --pack" in result.output
    assert "FAIL: the pack's settings file is missing" in result.output and lines(result)[-1].startswith("Result: FAIL")


def test_each_kind_of_damage_is_named_with_its_fix(config_dir, pack_dir, monkeypatch):
    cli.invoke(app, ["integrate", "--pack"])
    (config_dir / "agent" / "research-scout.md").unlink()
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 1 and "FAIL: files: missing agent/research-scout.md; run `gtu integrate --pack`" in result.output
    cli.invoke(app, ["integrate", "--pack"])
    lead = config_dir / "agent" / "research-lead.md"
    lead.write_text(lead.read_text(encoding="utf-8").replace("# Installed by spare-gpu", "# mine now"), encoding="utf-8")
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 0 and "WARN: edited by you (marker removed), so updates leave them alone: agent/research-lead.md" in result.output
    settings = config_dir / "config.json"
    doc = bc_config.parse_jsonc(settings.read_text(encoding="utf-8"))
    del doc["permission"]["task"]["research-scout"]
    doc["small_model"] = "bharatcode/deepseek-v4.1-flash"  # edited by hand
    settings.write_text(bc_config.PACK_LINE.join(["{\n  ", "\n"]) + json.dumps(doc, indent=2)[1:], encoding="utf-8")
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 1
    assert "FAIL: the settings file is stale or edited (small_model differ); run `gtu integrate --pack`" in result.output
    assert "FAIL: helpers not denied for your other agents (they cost tokens on every step): research-scout" in result.output
    cli.invoke(app, ["integrate", "--pack"])
    (config_dir / "opencode.jsonc").write_text(json.dumps({"mcp": {"spare-gpu": {"type": "local", "command": ["gtu", "mcp", "--pack"]}},
                                                           "small_model": "bharatcode/deepseek-v4.1-flash"}), encoding="utf-8")
    result = cli.invoke(app, ["pack-check"])
    assert result.exit_code == 0 and "WARN: conflict: opencode.jsonc sets small_model" in result.output
    monkeypatch.setenv("GTU_MODELS", "other-heavy,other-standard")
    assert "WARN: GTU_MODELS says other-heavy,other-standard but the files carry other ids" in cli.invoke(app, ["pack-check"]).output


def test_other_channels_without_the_pack_are_pointed_out(config_dir, pack_dir):
    (config_dir / "BharatCode Dev").mkdir()
    cli.invoke(app, ["integrate", "--pack", "--channel", "stable"])
    result = cli.invoke(app, ["pack-check"])
    assert "WARN: BharatCode Desktop's dev channel has a config folder without the pack: `gtu integrate --pack --channel dev`" in result.output
    assert cli.invoke(app, ["pack-check", "--channel", "nightly"]).exit_code == 1
    assert check.worst([check.Finding("PASS", "x")]) == "PASS" and check.worst([check.Finding("WARN", "x")]) == "WARN"
