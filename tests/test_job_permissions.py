"""What a job's BharatCode session may do: everything is denied first, then only reading and
searching its copy, editing when the job writes, and the listed commands. The user's own MCP
servers and spare-gpu's own are switched off inside the job. (The private copy's opencode.json
and the Desktop session's ruleset say the same.)"""

import json

import pytest

from gpu_token_utilization import bc_config
from gpu_token_utilization.agent import permission_config
from gpu_token_utilization.desktop import DesktopAgent, session_rules
from gpu_token_utilization.fakedesktop import FakeDesktop
GITHUB = {"type": "local", "command": ["gh-mcp", "serve"], "environment": {"GITHUB_TOKEN": "secret-token"}}
EXA = {"type": "remote", "url": "https://mcp.exa.ai/mcp", "headers": {"Authorization": "Bearer secret-token"}}


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """The user's global BharatCode config, in a folder of its own (GTU_BHARATCODE_CONFIG_DIR)."""
    folder = tmp_path / "bharatcode-config"
    folder.mkdir()
    monkeypatch.setenv(bc_config.CONFIG_DIR_ENV, str(folder))

    def write(config, channel="stable", name="opencode.jsonc"):
        target = folder if channel == "stable" else folder / bc_config.CHANNELS[channel]
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
        return target / name

    return write


def config_seen(desktop):
    """The opencode.json the job's session ran under, as the fake Desktop found it."""
    (session,) = desktop.sessions.values()
    return session["config"]


# --- the permission block -------------------------------------------------------------------

def test_everything_is_denied_first_then_only_what_a_job_needs():
    perm = permission_config(edit=True, commands=("pytest",), mcp_servers={})["permission"]
    assert list(perm)[0] == "*" and perm["*"] == "deny"  # BharatCode keeps key order; the last matching rule wins
    assert perm["read"] == {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"}
    assert perm["glob"] == perm["grep"] == perm["list"] == "allow"
    assert perm["edit"] == "allow" and list(perm["bash"]) == ["*", "pytest*"] and perm["bash"]["*"] == "deny"
    for name in ("task", "write", "webfetch", "websearch", "todowrite", "skill", "external_directory",
                 "doom_loop", "question", "lsp", "some_mcp_server_tool"):
        assert perm.get(name, perm["*"]) == "deny", name  # spelled out, or caught by "*"


def test_a_read_only_job_can_neither_edit_nor_run_anything():
    perm = permission_config(edit=False, commands=(), mcp_servers={})["permission"]
    assert perm["*"] == "deny" and perm["edit"] == "deny" and perm["bash"] == "deny"


def test_no_commands_means_no_shell_at_all():
    perm = permission_config(edit=True, commands=(), mcp_servers={})["permission"]
    assert perm["bash"] == "deny" and perm["edit"] == "allow"
    assert permission_config(edit=False, commands=(), mcp_servers={})["permission"]["edit"] == "deny"


def test_nothing_in_a_job_may_ask_since_nobody_answers():
    # seen 5 Nov: a job's session waited 10+ minutes on a prompt to read the main repo's .git/config
    perm = permission_config(edit=True, commands=("python -m pytest",), mcp_servers={})["permission"]
    for name in ("external_directory", "doom_loop", "question"):
        assert perm[name] == "deny"
    assert perm["skill"] == "deny"  # and with it the skill list, ~3.5k tokens a step
    rules = session_rules(edit=True, commands=("python -m pytest",))
    assert all(rule["action"] in ("allow", "deny") for rule in rules)
    read = [(r["pattern"], r["action"]) for r in rules if r["permission"] == "read"]
    assert read == [("*", "allow"), ("*.env", "deny"), ("*.env.*", "deny"), ("*.env.example", "allow")]  # last wins


def test_desktop_session_rules_start_with_the_global_deny():
    rules = session_rules(edit=True, commands=("pytest",))
    assert rules[0] == {"permission": "*", "pattern": "*", "action": "deny"}
    later = [(r["permission"], r["pattern"], r["action"]) for r in rules[1:]]
    for rule in (("read", "*", "allow"), ("glob", "*", "allow"), ("edit", "*", "allow"), ("bash", "pytest*", "allow")):
        assert rule in later, rule
    assert later.index(("read", "*", "allow")) < later.index(("read", "*.env", "deny"))  # last match wins
    assert rules[-1] == {"permission": "spare-gpu_*", "pattern": "*", "action": "deny"}


# --- the user's MCP servers -----------------------------------------------------------------

def test_the_users_servers_are_read_from_every_channel_and_a_broken_file_is_skipped(user_config):
    user_config({"mcp": {"github": GITHUB, "spare-gpu": {"type": "local", "command": ["gtu", "mcp"]}}})
    user_config({"mcp": {"exa": EXA, "github": {"type": "local", "command": ["other"]}}}, channel="dev")
    user_config("{ this is not json", channel="beta")
    servers = bc_config.user_mcp_servers()
    assert set(servers) == {"github", "spare-gpu", "exa"}
    assert servers["github"] == GITHUB and servers["exa"] == EXA  # the stable channel wins a clash


def test_no_config_means_no_servers(user_config):
    assert bc_config.user_mcp_servers() == {}


def test_jobs_switch_the_users_other_servers_off_without_repeating_their_secrets(tmp_path, user_config):
    user_config({"mcp": {"github": GITHUB, "exa": EXA}})
    desktop = FakeDesktop()
    desktop.agent().run(tmp_path, "x")
    mcp = config_seen(desktop)["mcp"]
    assert mcp["github"] == {"type": "local", "command": ["gh-mcp", "serve"], "enabled": False}
    assert mcp["exa"] == {"type": "remote", "url": "https://mcp.exa.ai/mcp", "enabled": False}
    assert mcp["spare-gpu"]["enabled"] is False
    assert "secret-token" not in json.dumps(mcp)
    assert not (tmp_path / "opencode.json").exists()  # and the file is gone afterwards


def test_servers_given_explicitly_are_used_instead_and_survive_a_model_switch(tmp_path, user_config):
    user_config({"mcp": {"github": GITHUB}})  # not read: the caller said which servers there are
    desktop = FakeDesktop()
    agent = desktop.agent(mcp_servers={"linear": {"type": "remote", "url": "https://x"}})
    agent.with_model("bharatcode/qwen-3.8-27b").run(tmp_path, "x")
    mcp = config_seen(desktop)["mcp"]
    assert set(mcp) == {"linear", "spare-gpu"} and mcp["linear"]["enabled"] is False


def test_a_desktop_agent_keeps_the_servers_through_a_model_switch():
    agent = DesktopAgent(conn=object(), mcp_servers={"linear": {"type": "remote", "url": "https://x"}})
    assert agent.with_model("bharatcode/qwen-3.8-27b").mcp_servers == {"linear": {"type": "remote", "url": "https://x"}}
    assert DesktopAgent(conn=object()).mcp_servers is None  # the default: read the user's config


def test_jobs_switch_spare_gpus_own_mcp_server_off():
    # measured: its tool schemas add ~2.3k tokens to every step, and a job shouldn't queue jobs
    assert permission_config(edit=True, commands=(), mcp_servers={})["mcp"]["spare-gpu"]["enabled"] is False
