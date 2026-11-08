"""Memory across chats: the pack's `memory` component points Desktop's `instructions` at
.gtu/memory.md, its three commands run in the user's own agent, git sees the memory file once
spare-gpu's store has made the .gtu folder, and the doctor shows what the file costs."""

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, doctor, pack, settings
from gpu_token_utilization.cli import app
from gpu_token_utilization.mcp_server import SpareTools
from gpu_token_utilization.pack import check
from gpu_token_utilization.store import Store

from conftest import git
from test_doctor import config_dir, research  # noqa: F401  fixtures

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
    # Desktop runs !`...` lines outside the project folder, so the chat runs git itself, in the project
    assert "!`" not in body and "`git status --porcelain`" in body and ".gtu/handoff.md" in body and "/pickup" in body
    for section in ("## Goal", "## Done", "## Next", "## Open questions", "## Files", "## Pointers"):
        assert section in body
    pickup, body = command("pickup")
    assert "agent" not in pickup and ".gtu/handoff.md" in body and "Otherwise stop and wait" in body
    assert "!`" not in body and "`git status --porcelain`" in body
    cost, body = command("cost")  # no shell command any more: one tool call through spare-helper
    assert "!`" not in body and "`cost_summary`" in body and "gtu" not in body and cost["agent"] == "spare-helper"
    assert all(("command", name) in pack.all_markdown_names() for name in ("remember", "handoff", "pickup"))


def test_install_and_pack_check_know_the_component(config_dir, project):
    result = cli.invoke(app, ["integrate", "--pack", "--only", "memory"])
    assert result.exit_code == 0, result.output
    for name in ("remember", "handoff", "pickup"):
        assert (config_dir / "command" / f"{name}.md").is_file()
    doc = bc_config.parse_jsonc((config_dir / "config.json").read_text(encoding="utf-8"))
    assert doc["instructions"] == [".gtu/memory.md"]
    assert check.installed_components(config_dir) == ["memory"]
    assert check.worst(check.run(config_dir) + check.check_channels()) != "FAIL"
    stale = (config_dir / "config.json").read_text(encoding="utf-8").replace('".gtu/memory.md"', '"elsewhere.md"')
    (config_dir / "config.json").write_text(stale, encoding="utf-8")
    assert any(str(f).startswith("FAIL: the settings file is stale or edited (instructions differ)") for f in check.run(config_dir))


def test_git_sees_the_memory_file_but_not_the_handoff_once_the_store_made_the_folder(project):
    with Store(project / ".gtu" / "gtu.db"):  # the store writes .gtu/.gitignore itself
        pass
    assert (project / ".gtu" / ".gitignore").read_text(encoding="utf-8") == settings.IGNORE
    (project / ".gtu" / "memory.md").write_text("# Project memory\n- tests run with pytest -q\n", encoding="utf-8")
    (project / ".gtu" / "handoff.md").write_text("## Goal\nx\n", encoding="utf-8")
    (project / ".gtu" / "settings.json").write_text("{}", encoding="utf-8")
    status = git(project, "status", "--porcelain", "--untracked-files=all")
    assert ".gtu/memory.md" in status and ".gtu/settings.json" in status and "handoff.md" not in status and "gtu.db" not in status
    assert settings.IGNORE.endswith("!memory.md\n")


@pytest.mark.parametrize("old", ["*\n", "*", "*\n!.gitignore\n!settings.json\n"])
def test_the_store_upgrades_an_earlier_versions_ignore_file_but_not_the_users(project, old):
    folder = project / ".gtu"
    folder.mkdir()
    (folder / ".gitignore").write_text(old, encoding="utf-8")  # what earlier versions wrote
    with Store(folder / "gtu.db"):
        pass
    assert (folder / ".gitignore").read_text(encoding="utf-8") == settings.IGNORE
    (folder / ".gitignore").write_text("*\n!mine.txt\n", encoding="utf-8")  # the user's own
    with Store(folder / "gtu.db"):
        pass
    assert (folder / ".gitignore").read_text(encoding="utf-8") == "*\n!mine.txt\n"


def test_a_store_outside_a_gtu_folder_ignores_nothing(tmp_path):
    with Store(tmp_path / "elsewhere" / "gtu.db"):  # GTU_DB pointing somewhere: never git-ignore that folder
        pass
    assert not (tmp_path / "elsewhere" / ".gitignore").exists()


def memory_lines(project, folder):
    report = doctor.Report()
    doctor.check_memory(report, project, folder)
    return "\n".join(str(f) for f in report.findings)


def test_the_doctor_shows_the_memory_files_cost_and_the_cap(config_dir, project):
    cli.invoke(app, ["integrate", "--pack", "--only", "memory"])
    assert "PASS: no project memory yet; /remember writes .gtu/memory.md" in memory_lines(project, config_dir)
    (project / ".gtu").mkdir(exist_ok=True)
    small = "# Project memory\n- a\n- b\n"
    (project / ".gtu" / "memory.md").write_text(small, encoding="utf-8")
    assert (f"PASS: project memory .gtu/memory.md: 3 lines, {len(small)} characters (about {len(small) // 4} tokens on "
            "every step in this project)") in memory_lines(project, config_dir)
    big = "# Project memory\n" + "- a long remembered fact about the project\n" * 120
    (project / ".gtu" / "memory.md").write_text(big, encoding="utf-8")
    assert f"WARN: project memory .gtu/memory.md is {len(big):,} characters (121 lines), over the 4,000 cap" in memory_lines(project, config_dir)
    cli.invoke(app, ["integrate", "--pack", "--uninstall"])
    assert "WARN: project memory .gtu/memory.md exists but the pack's memory component isn't installed" in memory_lines(project, config_dir)
    # and the whole /doctor answer carries it
    assert "WARN: project memory .gtu/memory.md exists" in SpareTools(project).doctor_check()
