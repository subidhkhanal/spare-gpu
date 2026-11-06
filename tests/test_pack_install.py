"""`gtu integrate --pack`: the pack's settings live in a config file of their own (BharatCode merges
config.json, opencode.json and opencode.jsonc), its commands and agents are copied with the
marker and the model ids filled in, helpers and pack-only tools are hidden from every other
agent, conflicts with the user's own settings are reported, and --uninstall removes exactly
what was added. No live calls: model ids come from GTU_MODELS or the fake API."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from gpu_token_utilization import bc_config, pack
from gpu_token_utilization.bc_config import ConfigError, parse_jsonc
from gpu_token_utilization.cli import app

cli = CliRunner()
HEAVY, STANDARD = "bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b"
LEAD = "---\n# Installed by spare-gpu\ndescription: lead\nmode: primary\nmodel: {{HEAVY_MODEL}}\n---\nLead on {{HEAVY_MODEL}}.\n"
SCOUT = "---\n# Installed by spare-gpu\ndescription: scout\nmode: subagent\nmodel: {{STANDARD_MODEL}}\n---\nScout.\n"
COMMAND = "---\n# Installed by spare-gpu\ndescription: research\nagent: research-lead\n---\n$ARGUMENTS\n"


@pytest.fixture(autouse=True)
def known_models(monkeypatch):
    """Model ids without the network (the fake API covers the other path below)."""
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
    """A pack with one research command, a lead and a scout (the real one fills up step by step)."""
    source = tmp_path / "pack"
    (source / "commands").mkdir(parents=True)
    (source / "agents").mkdir()
    (source / "commands" / "deep-research.md").write_text(COMMAND, encoding="utf-8")
    (source / "agents" / "research-lead.md").write_text(LEAD, encoding="utf-8")
    (source / "agents" / "research-scout.md").write_text(SCOUT, encoding="utf-8")
    monkeypatch.setattr(pack, "PACK_DIR", source)
    monkeypatch.setattr(pack.markdown_files, "__defaults__", (source,))
    monkeypatch.setattr(pack.helpers, "__defaults__", (source,))
    monkeypatch.setattr(pack.config, "__defaults__", (source,))
    return source


def models():
    return pack.Models(HEAVY, STANDARD)


# --- the pack itself --------------------------------------------------------------------------

def test_components_are_selected_by_name_and_unknown_ones_refused():
    assert [c.name for c in pack.select(None)] == list(pack.COMPONENTS)
    assert [c.name for c in pack.select("review, split,review")] == ["review", "split"]
    with pytest.raises(pack.PackError, match="unknown pack component 'bogus'"):
        pack.select("bogus")


def test_the_pack_only_installs_files_that_exist_and_knows_its_helpers(pack_dir):
    chosen = pack.select("research,review")
    found = [(kind, name) for kind, name, _ in pack.markdown_files(chosen)]
    assert found == [("command", "deep-research"), ("agent", "research-lead"), ("agent", "research-scout")]
    assert pack.helpers(chosen) == ["research-scout"]  # the lead is primary: never offered as a subagent


def test_the_config_file_splits_models_and_hides_helpers_and_pack_tools(pack_dir):
    doc = pack.config(pack.select(None), models())
    assert doc["small_model"] == STANDARD and doc["agent"] == {"title": {"model": STANDARD}, "explore": {"model": STANDARD}}
    assert doc["permission"]["task"] == {"research-scout": "deny"}
    for tool in ("scholar_search", "verify_refs", "context_report"):
        assert doc["permission"][f"spare-gpu_{tool}"] == "deny"
    assert doc["mcp"] == {"exa": {"type": "remote", "url": "https://mcp.exa.ai/mcp"}}
    text = pack.config_text(doc, bc_config.PACK_LINE)
    assert text.startswith("{\n  " + bc_config.PACK_LINE + "\n") and parse_jsonc(text) == doc
    assert "titles and explore on Qwen 3.8 27B" in pack.describe(doc) and "MCP: exa" in pack.describe(doc)


def test_without_the_split_or_research_the_config_file_is_smaller(pack_dir):
    doc = pack.config(pack.select("review"), models())
    assert "small_model" not in doc and "agent" not in doc and "mcp" not in doc and "permission" not in doc
    assert pack.describe(doc) == "nothing yet"


def test_model_ids_are_filled_in_and_lite_puts_every_role_on_the_standard_model():
    assert models().fill(LEAD).count(HEAVY) == 2 and "{{" not in models().fill(LEAD)
    assert pack.Models(STANDARD, STANDARD).fill(LEAD).count(STANDARD) == 2


def test_models_come_from_bharatcodes_public_list_or_the_known_ids(fake, monkeypatch):
    monkeypatch.delenv("GTU_MODELS")
    chosen, note = pack.resolve_models(client=httpx.Client(transport=fake.transport()))
    assert (chosen.heavy, chosen.standard) == (HEAVY, STANDARD) and note == ""
    assert pack.resolve_models(client=httpx.Client(transport=fake.transport()), lite=True)[0].heavy == STANDARD
    down = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    chosen, note = pack.resolve_models(client=down)
    assert (chosen.heavy, chosen.standard) == (HEAVY, STANDARD) and "using the known ids" in note


# --- the pack's config file next to the user's ---------------------------------------------------

def test_the_pack_file_is_config_json_and_the_users_file_is_never_touched(config_dir):
    doc = pack.config(pack.select("split"), models())
    path, outcome = bc_config.install_pack_config(config_dir, pack.config_text(doc, bc_config.PACK_LINE))
    assert path == config_dir / "config.json" and outcome == "added" and bc_config.is_pack_file(path)
    assert (config_dir / "opencode.jsonc").read_text(encoding="utf-8") == '{\n  "$schema": "https://opencode.ai/config.json"\n}'
    assert bc_config.config_file(config_dir) == config_dir / "opencode.jsonc"  # the pack's file is never "the config"
    assert bc_config.install_pack_config(config_dir, pack.config_text(doc, bc_config.PACK_LINE))[1] == "unchanged"


def test_when_the_user_owns_config_json_the_pack_takes_opencode_json(config_dir):
    (config_dir / "opencode.jsonc").unlink()
    (config_dir / "config.json").write_text('{"theme": "dark"}', encoding="utf-8")
    assert bc_config.pack_file(config_dir) == config_dir / "opencode.json"
    (config_dir / "opencode.json").write_text('{"theme": "light"}', encoding="utf-8")  # theirs too
    with pytest.raises(ConfigError, match="no file left for the pack"):
        bc_config.pack_file(config_dir)


def test_a_missing_user_file_is_created_first_so_bharatcode_writes_its_own_settings_there(tmp_path):
    folder = tmp_path / "Config"
    folder.mkdir()
    bc_config.install_pack_config(folder, pack.config_text(pack.config(pack.select("split"), models()), bc_config.PACK_LINE))
    assert json.loads((folder / "opencode.jsonc").read_text(encoding="utf-8")) == {"$schema": "https://opencode.ai/config.json"}
    assert bc_config.is_pack_file(folder / "config.json")


def test_uninstall_removes_the_pack_file_but_not_one_the_user_took_over(config_dir):
    path, _ = bc_config.install_pack_config(config_dir, pack.config_text(pack.config(pack.select("split"), models()), bc_config.PACK_LINE))
    assert bc_config.remove_pack_config(config_dir) == (path, "removed") and not path.exists()
    assert bc_config.remove_pack_config(config_dir)[1] == "absent"
    path.write_text('{"theme": "dark"}', encoding="utf-8")  # no marker: it's the user's now
    assert bc_config.remove_pack_config(config_dir)[1] == "absent" and path.exists()


def test_conflicts_with_the_users_own_settings_are_reported_with_who_wins(config_dir, pack_dir):
    (config_dir / "opencode.jsonc").write_text(json.dumps({
        "small_model": "bharatcode/deepseek-v4.1-flash",
        "agent": {"title": {"model": STANDARD}},  # the same value: no conflict
        "permission": {"task": {"research-scout": "allow"}},
        "mcp": {"exa": {"type": "remote", "url": "https://elsewhere"}},
    }), encoding="utf-8")
    doc = pack.config(pack.select(None), models())
    bc_config.install_pack_config(config_dir, pack.config_text(doc, bc_config.PACK_LINE))
    found = bc_config.pack_conflicts(config_dir, doc)
    assert any(c.startswith("opencode.jsonc sets small_model = \"bharatcode/deepseek-v4.1-flash\"") and "yours wins" in c for c in found)
    assert any("permission.task.research-scout" in c for c in found) and any("mcp.exa" in c for c in found)
    assert not any("agent.title" in c for c in found) and len(found) == 3


# --- gtu integrate --pack -------------------------------------------------------------------------

def test_integrate_pack_installs_everything_and_says_what_it_did(config_dir, pack_dir):
    result = cli.invoke(app, ["integrate", "--pack"])
    out = result.output
    assert result.exit_code == 0, out
    assert "Pack models: heavy DeepSeek V4.1 Flash (bharatcode/deepseek-v4.1-flash), standard Qwen 3.8 27B" in out
    assert f"Wrote the pack's settings in {config_dir / 'config.json'}: titles and explore on Qwen 3.8 27B; " in out
    assert "1 helper agent(s) hidden" in out and "3 pack-only tool(s) hidden" in out and "MCP: exa" in out
    assert "Added /deep-research" in out and "Added the research-lead agent" in out and "Added the research-scout agent" in out
    assert "To undo the pack: gtu integrate --pack --uninstall" in out
    lead = (config_dir / "agent" / "research-lead.md").read_text(encoding="utf-8")
    assert "# Installed by spare-gpu" in lead and "model: bharatcode/deepseek-v4.1-flash" in lead and "{{" not in lead
    assert "model: bharatcode/qwen-3.8-27b" in (config_dir / "agent" / "research-scout.md").read_text(encoding="utf-8")
    assert (config_dir / "command" / "deep-research.md").is_file() and (config_dir / "command" / "auto.md").is_file()
    settings = parse_jsonc((config_dir / "config.json").read_text(encoding="utf-8"))
    assert settings["permission"]["task"] == {"research-scout": "deny"} and settings["mcp"]["exa"]["type"] == "remote"
    assert bc_config.parse_jsonc((config_dir / "opencode.jsonc").read_text(encoding="utf-8")).get("mcp", {}).get("spare-gpu")
    again = cli.invoke(app, ["integrate", "--pack"])
    assert "Already up to date: the pack's settings" in again.output and "/deep-research is already there" in again.output


def test_integrate_pack_with_only_split_or_lite(config_dir, pack_dir):
    result = cli.invoke(app, ["integrate", "--pack", "--only", "split"])
    assert result.exit_code == 0 and "No commands or agents to add for: split" in result.output
    assert not (config_dir / "agent").exists()
    settings = parse_jsonc((config_dir / "config.json").read_text(encoding="utf-8"))
    assert settings["small_model"] == STANDARD and "permission" not in settings and "mcp" not in settings
    lite = cli.invoke(app, ["integrate", "--pack", "--only", "research", "--lite"])
    assert lite.exit_code == 0 and "heavy Qwen 3.8 27B (bharatcode/qwen-3.8-27b)" in lite.output
    assert "model: bharatcode/qwen-3.8-27b" in (config_dir / "agent" / "research-lead.md").read_text(encoding="utf-8")
    assert cli.invoke(app, ["integrate", "--pack", "--only", "bogus"]).exit_code == 1


def test_integrate_pack_reports_conflicts_and_keeps_the_users_own_files(config_dir, pack_dir):
    (config_dir / "opencode.jsonc").write_text('{"small_model": "bharatcode/deepseek-v4.1-flash"}', encoding="utf-8")
    (config_dir / "agent").mkdir()
    (config_dir / "agent" / "research-lead.md").write_text("---\ndescription: mine\n---\nmy lead\n", encoding="utf-8")
    result = cli.invoke(app, ["integrate", "--pack"])
    assert result.exit_code == 0, result.output
    assert "Conflict: opencode.jsonc sets small_model" in result.output and "yours wins" in result.output
    assert "You already have the research-lead agent" in result.output
    assert (config_dir / "agent" / "research-lead.md").read_text(encoding="utf-8") == "---\ndescription: mine\n---\nmy lead\n"


def test_pack_uninstall_removes_only_the_pack_and_plain_uninstall_removes_all(config_dir, pack_dir):
    cli.invoke(app, ["integrate", "--pack"])
    result = cli.invoke(app, ["integrate", "--pack", "--uninstall"])
    assert result.exit_code == 0, result.output
    assert "Removed /deep-research" in result.output and "Removed the research-scout agent" in result.output
    assert "Removed the pack's settings file" in result.output
    assert not (config_dir / "config.json").exists() and not (config_dir / "agent" / "research-lead.md").exists()
    assert (config_dir / "command" / "auto.md").is_file()  # /auto and the MCP server stay
    assert bc_config.parse_jsonc((config_dir / "opencode.jsonc").read_text(encoding="utf-8"))["mcp"]["spare-gpu"]
    again = cli.invoke(app, ["integrate", "--pack", "--uninstall"])
    assert "wasn't installed" in again.output
    cli.invoke(app, ["integrate", "--pack"])
    everything = cli.invoke(app, ["integrate", "--uninstall"])
    assert "Removed the pack's settings file" in everything.output and "Removed spare-gpu from" in everything.output
    assert not (config_dir / "config.json").exists() and not (config_dir / "command" / "auto.md").exists()
