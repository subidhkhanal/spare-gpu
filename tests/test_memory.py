"""Memory across chats: the pack's `memory` component points Desktop's `instructions` at
.gtu/memory.md, its three commands run in the user's own agent, git sees the memory file after
`gtu init`, and the doctor shows what the file costs."""

import json

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, doctor, pack, settings
from gpu_token_utilization.cli import app
from gpu_token_utilization.pack import check

from conftest import git
from test_doctor import config_dir, live_cli, merged_config, research, result_cell  # noqa: F401  fixtures

cli = CliRunner()
MODELS = pack.Models("bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b")


@pytest.fixture(autouse=True)
def known_models(monkeypatch):
    monkeypatch.setenv("GTU_MODELS", "deepseek-v4.1-flash,qwen-3.8-27b")


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    return git_repo


def command(name):
    path = pack.PACK_DIR / "commands" / f"{name}.md"
    text = MODELS.fill(path.read_text(encoding="utf-8"))
    head, body = text.split("\n---\n", 1)
    return pack.frontmatter(text), body


def test_the_memory_component_has_three_commands_no_agents_and_points_instructions_at_the_file():
    component = pack.COMPONENTS["memory"]
    assert component.commands == ("remember", "handoff", "pickup") and not component.agents and not component.tools
    doc = pack.config(pack.select("memory"), MODELS)
    assert doc["instructions"] == [".gtu/memory.md"] and "permission" not in doc and "mcp" not in doc
    assert pack.describe(doc) == ".gtu/memory.md read by every agent"
    assert "instructions" not in pack.config(pack.select("review"), MODELS)
    assert pack.pack_added_tokens(pack.config(pack.select(None), MODELS)) == 0  # the file's own size is the user's, capped by advice


def test_the_commands_run_in_the_users_agent_and_say_what_they_may_not_do():
    remember, body = command("remember")
    assert "agent" not in remember and remember["model"] == MODELS.standard
    assert ".gtu/memory.md" in body and "Never delete lines on your own" in body and "no secrets" in body
    assert "4,000 characters" in body and "60 lines" in body
    handoff, body = command("handoff")
    assert "agent" not in handoff and "model" not in handoff  # the current chat's agent and model: it needs the history
    assert "!`git status --porcelain`" in body and ".gtu/handoff.md" in body and "/pickup" in body
    for section in ("## Goal", "## Done", "## Next", "## Open questions", "## Files", "## Pointers"):
        assert section in body
    pickup, body = command("pickup")
    assert "agent" not in pickup and ".gtu/handoff.md" in body and "Otherwise stop and wait" in body
    assert all(("command", name) in pack.all_markdown_names() for name in ("remember", "handoff", "pickup"))


def test_install_and_pack_check_know_the_component(config_dir, project):
    result = cli.invoke(app, ["integrate", "--pack", "--only", "memory"])
    assert result.exit_code == 0, result.output
    for name in ("remember", "handoff", "pickup"):
        assert (config_dir / "command" / f"{name}.md").is_file()
    doc = bc_config.parse_jsonc((config_dir / "config.json").read_text(encoding="utf-8"))
    assert doc["instructions"] == [".gtu/memory.md"]
    assert check.installed_components(config_dir) == ["memory"]
    assert cli.invoke(app, ["pack-check"]).exit_code == 0
    stale = (config_dir / "config.json").read_text(encoding="utf-8").replace('".gtu/memory.md"', '"elsewhere.md"')
    (config_dir / "config.json").write_text(stale, encoding="utf-8")
    assert "FAIL: the settings file is stale or edited (instructions differ)" in cli.invoke(app, ["pack-check"]).output


def test_git_sees_the_memory_file_but_not_the_handoff_after_init(project):
    cli.invoke(app, ["init"])
    (project / ".gtu" / "memory.md").write_text("# Project memory\n- tests run with pytest -q\n", encoding="utf-8")
    (project / ".gtu" / "handoff.md").write_text("## Goal\nx\n", encoding="utf-8")
    status = git(project, "status", "--porcelain", "--untracked-files=all")
    assert ".gtu/memory.md" in status and ".gtu/settings.json" in status and "handoff.md" not in status
    assert settings.IGNORE.endswith("!memory.md\n")


def test_the_doctor_shows_the_memory_files_cost_and_the_cap(config_dir, project):
    cli.invoke(app, ["integrate", "--pack", "--only", "memory"])
    out = cli.invoke(app, ["doctor"]).output
    assert "PASS: no project memory yet; /remember writes .gtu/memory.md" in out
    (project / ".gtu").mkdir(exist_ok=True)
    small = "# Project memory\n- a\n- b\n"
    (project / ".gtu" / "memory.md").write_text(small, encoding="utf-8")
    out = cli.invoke(app, ["doctor"]).output
    assert f"PASS: project memory .gtu/memory.md: 3 lines, {len(small)} characters (about {len(small) // 4} tokens on every step in this project)" in out
    big = "# Project memory\n" + "- a long remembered fact about the project\n" * 120
    (project / ".gtu" / "memory.md").write_text(big, encoding="utf-8")
    out = cli.invoke(app, ["doctor"]).output
    assert f"WARN: project memory .gtu/memory.md is {len(big):,} characters (121 lines), over the 4,000 cap" in out
    cli.invoke(app, ["integrate", "--pack", "--uninstall"])
    out = cli.invoke(app, ["doctor"]).output
    assert "WARN: project memory .gtu/memory.md exists but the pack's memory component isn't installed" in out


def test_live_confirms_desktop_reads_the_memory_file(config_dir, project, live_cli, research):
    (research / "memory").mkdir()
    (research / "memory" / "manual-check.md").write_text(
        (doctor.RESEARCH.parent / "research" / "memory" / "manual-check.md").read_text(encoding="utf-8")
        if (doctor.RESEARCH.parent / "research" / "memory" / "manual-check.md").is_file() else
        "| # | Check | Steps | Pass condition | Result |\n|---|---|---|---|---|\n| 1 | Desktop reads the memory file | x | y | |\n",
        encoding="utf-8")
    cli.invoke(app, ["integrate", "--pack", "--only", "memory"])
    live_cli()
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, result.output
    assert "PASS: live: Desktop's merged config reads .gtu/memory.md with every request" in result.output
    assert result_cell(research, "memory", 1).startswith("PASS (gtu doctor --live")
    merged = merged_config(config_dir)
    merged["instructions"] = ["AGENTS.md"]  # the user's own file overrides the pack's list
    live_cli(merged=merged)
    result = cli.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 1 and "FAIL: live: Desktop's merged config doesn't list .gtu/memory.md under instructions" in result.output
