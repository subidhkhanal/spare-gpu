"""/context-report's numbers: the inventory counts what the config and files hold and prices each
item with the measured costs; a live measurement reads the CLI's step_finish event (the fake CLI
here); usage comes from `bharatcode stats` when there is one; a switch is applied only after a
backup; and the text names the exact switches."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, overhead
from gpu_token_utilization.cli import app

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]
runner = CliRunner()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A config folder with spare-gpu and a github server, the pack installed, a home with 3
    skills, and a project with an AGENTS.md."""
    folder = tmp_path / "Config"
    folder.mkdir()
    (folder / "opencode.jsonc").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"spare-gpu": {"type": "local", "command": ["gtu", "mcp", "--pack"]},
                "github": {"type": "remote", "url": "https://x"}, "old": {"type": "local", "command": ["x"], "enabled": False}},
    }), encoding="utf-8")
    monkeypatch.setenv(bc_config.CONFIG_DIR_ENV, str(folder))
    from gpu_token_utilization import pack
    doc = pack.config(pack.select(None), pack.Models("bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b"))
    bc_config.install_pack_config(folder, pack.config_text(doc, bc_config.PACK_LINE))
    home = tmp_path / "home"
    for name in ("a", "b"):
        (home / ".claude" / "skills" / name).mkdir(parents=True)
        (home / ".claude" / "skills" / name / "SKILL.md").write_text("---\nname: x\n---\n")
    (home / ".agents" / "skills" / "c").mkdir(parents=True)
    (home / ".agents" / "skills" / "c" / "SKILL.md").write_text("x")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("x" * 3600, encoding="utf-8")
    return folder, home, project


def test_the_inventory_counts_and_prices_every_item(setup):
    folder, home, project = setup
    report = overhead.inventory(project, home=home)
    by_kind = {}
    for item in report.items:
        by_kind.setdefault(item.kind, []).append(item)
    assert by_kind["base"][0].tokens == 8_000
    mcp = {i.name: i for i in by_kind["mcp"]}
    assert "MCP server spare-gpu (local)" in mcp and mcp["MCP server spare-gpu (local)"].tokens == 5 * 330
    assert mcp["MCP server github (remote)"].tokens is None and "bharatcode mcp list" in mcp["MCP server github (remote)"].how
    assert not any("old" in name for name in mcp)  # disabled servers cost nothing
    exa = mcp["MCP server exa (remote)"]  # the pack's own: never edited in place
    assert exa.apply is None and "--only split, review, security, simplify, context" in exa.switch
    assert mcp["MCP server github (remote)"].apply == {"file": str(folder / "opencode.jsonc"), "mcp_disable": "github"}
    skills = by_kind["skills"][0]
    assert skills.name.startswith("3 skill(s) (3 from ~/.claude/skills") and skills.tokens == 3 * 220
    assert skills.apply == {"file": str(folder / "opencode.jsonc"), "permission": {"skill": "deny"}}
    agents_md = by_kind["instructions"][0]
    assert agents_md.name.startswith("AGENTS.md (3,600 bytes)") and agents_md.tokens == 1000 and agents_md.apply is None
    assert by_kind["pack"][0].tokens == 0 and "by design" in by_kind["pack"][0].how
    assert report.estimate() == 8_000 + 1_650 + 660 + 1_000
    assert [i.name for i in report.waste()][:3] == [mcp["MCP server spare-gpu (local)"].name, agents_md.name, skills.name]
    assert report.waste()[-1].tokens is None  # unknown sizes last
    assert folder / "config.json" in report.config_files


def test_denied_skills_and_a_missing_pack_are_reported(setup):
    folder, home, project = setup
    (folder / "opencode.jsonc").write_text(json.dumps({"permission": {"skill": "deny"}, "mcp": {}}), encoding="utf-8")
    (folder / "config.json").unlink()
    report = overhead.inventory(project, home=home)
    skills = next(i for i in report.items if i.kind == "skills")
    assert skills.tokens == 0 and skills.switch is None
    assert not any(i.kind == "pack" for i in report.items) and "isn't installed" in report.notes[0]


def test_a_live_measurement_reads_the_clis_step_finish(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": [{"tokens": 11_500}], "text": "OK"}))
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(scenario))
    measured = overhead.measure(tmp_path, command=FAKE_CLI)
    assert measured == {"context": 11_450, "output": 50, "steps": 1}
    assert overhead.measure(tmp_path, command=[sys.executable, "-c", "import sys; sys.exit(3)"]) is None
    monkeypatch.delenv("GTU_BHARATCODE_CMD")
    monkeypatch.setattr("gpu_token_utilization.agent_cli.shutil.which", lambda name: None)
    assert overhead.measure(tmp_path) is None  # no CLI at all


def test_usage_comes_from_the_cli_or_is_none():
    assert overhead.usage(7, command=FAKE_CLI) is None  # the fake has no stats command
    echo = [sys.executable, "-c", "import sys; print('\\x1b[32mTotal tokens: 12,345\\x1b[0m'); print('qwen: 9,000')"]
    assert overhead.usage(7, command=echo) == "Total tokens: 12,345\nqwen: 9,000"


def test_bharatcode_stats_model_usage_is_parsed_with_its_rounding():
    from test_mcp import STATS

    figures = overhead.parse_stats(STATS)
    assert set(figures) == {"bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b"}
    deepseek = figures["bharatcode/deepseek-v4.1-flash"]
    assert deepseek["messages"] == 12 and deepseek["input"] == 120_500 and deepseek["cache_read"] == 40_000
    assert deepseek["tokens"] == 120_500 + 8_200 + 40_000
    assert overhead.parse_stats("no model usage section at all") == {}
    assert overhead._stats_number("1.2M") == 1_200_000 and overhead._stats_number("$0.0012") == 0.0012


def test_apply_backs_the_file_up_first_and_changes_only_the_switch(setup):
    folder, home, project = setup
    report = overhead.inventory(project, home=home)
    github = next(i for i in report.items if "github" in i.name)
    backup = overhead.apply(github, now=datetime(2026, 11, 6, 12, 30, 0))
    assert backup == folder / "opencode.jsonc.20261106-123000.spare-gpu-backup" and '"github"' in backup.read_text()
    config = json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8"))
    assert config["mcp"]["github"] == {"type": "remote", "url": "https://x", "enabled": False}
    assert config["mcp"]["spare-gpu"]["command"] == ["gtu", "mcp", "--pack"]  # untouched
    skills = next(i for i in report.items if i.kind == "skills")
    overhead.apply(skills, now=datetime(2026, 11, 6, 12, 31, 0))
    assert json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8"))["permission"] == {"skill": "deny"}
    with pytest.raises(ValueError, match="by hand"):
        overhead.apply(next(i for i in report.items if i.kind == "instructions"))


def test_the_text_names_each_switch_and_the_apply_command(setup):
    folder, home, project = setup
    text = overhead.describe(overhead.inventory(project, home=home))
    assert text.startswith(f"Per-step overhead in {project}")
    assert "  1,650 tokens  MCP server spare-gpu (local): 5 tools x 330 tokens (measured)" in text
    assert "Estimate: about 11,310 tokens per step, plus 2 item(s) of unknown size." in text  # github and the pack's exa
    assert '1. MCP server spare-gpu (local): 1,650 tokens. Switch: in ' in text and "--apply 1" in text
    assert "then /auto stops working" in text and "shorten" in text and "unknown size" in text


def test_gtu_context_report_prints_and_applies_with_confirmation(setup, monkeypatch):
    folder, home, project = setup
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    result = runner.invoke(app, ["context-report"])
    assert result.exit_code == 0, result.output
    assert "Per-step overhead" in result.output and "Top savings" in result.output
    declined = runner.invoke(app, ["context-report", "--apply", "1"], input="n\n")
    assert declined.exit_code == 0 and "Nothing changed." in declined.output
    assert json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8"))["mcp"]["spare-gpu"].get("enabled") is None
    applied = runner.invoke(app, ["context-report", "--apply", "1", "--yes"])
    assert applied.exit_code == 0 and "Done. The file as it was is saved as" in applied.output
    assert json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8"))["mcp"]["spare-gpu"]["enabled"] is False
    assert runner.invoke(app, ["context-report", "--apply", "99"]).exit_code == 1
