"""What a job's BharatCode session may do: everything is denied first, then only reading and
searching its copy, editing when the job writes, and the listed commands. The user's own MCP
servers are switched off inside the job, and their config overrides in the environment don't
reach it. (The private copy's opencode.json and the Desktop session's ruleset say the same.)"""

import json
import sys

import pytest

from gpu_token_utilization import bc_config
from gpu_token_utilization.agent_cli import JOB_ENV_DROPPED, BharatCodeCli, job_env, permission_config
from gpu_token_utilization.desktop import DesktopAgent, session_rules

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]
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


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """What the fake CLI saw: its arguments, the opencode.json next to it, and its environment."""
    scenario, record = tmp_path / "scenario.json", tmp_path / "calls.jsonl"
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(scenario))
    scenario.write_text(json.dumps({"record": str(record)}))
    return lambda: json.loads(record.read_text().splitlines()[0])


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


def test_no_config_means_no_servers_and_nothing_is_started_to_find_out(user_config, monkeypatch):
    monkeypatch.delenv("GTU_BHARATCODE_CMD")  # a CLI start would fail loudly here
    assert bc_config.user_mcp_servers() == {}


def test_jobs_switch_the_users_other_servers_off_without_repeating_their_secrets(tmp_path, recorded, user_config):
    user_config({"mcp": {"github": GITHUB, "exa": EXA}})
    BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x")
    mcp = recorded()["config"]["mcp"]
    assert mcp["github"] == {"type": "local", "command": ["gh-mcp", "serve"], "enabled": False}
    assert mcp["exa"] == {"type": "remote", "url": "https://mcp.exa.ai/mcp", "enabled": False}
    assert mcp["spare-gpu"]["enabled"] is False
    assert "secret-token" not in json.dumps(mcp)
    assert not (tmp_path / "opencode.json").exists()  # and the file is gone afterwards


def test_servers_given_explicitly_are_used_instead_and_survive_a_model_switch(tmp_path, recorded, user_config):
    user_config({"mcp": {"github": GITHUB}})  # not read: the caller said which servers there are
    cli = BharatCodeCli(command=FAKE_CLI, mcp_servers={"linear": {"type": "remote", "url": "https://x"}})
    cli.with_model("bharatcode/qwen-3.8-27b").run(tmp_path, "x")
    mcp = recorded()["config"]["mcp"]
    assert set(mcp) == {"linear", "spare-gpu"} and mcp["linear"]["enabled"] is False


def test_a_desktop_agent_keeps_the_servers_through_a_model_switch():
    agent = DesktopAgent(conn=object(), mcp_servers={"linear": {"type": "remote", "url": "https://x"}})
    assert agent.with_model("bharatcode/qwen-3.8-27b").mcp_servers == {"linear": {"type": "remote", "url": "https://x"}}
    assert DesktopAgent(conn=object()).mcp_servers is None  # the default: read the user's config


# --- the environment ------------------------------------------------------------------------

def test_a_config_override_in_the_environment_never_reaches_a_job(tmp_path, recorded, user_config, monkeypatch):
    for name in JOB_ENV_DROPPED:
        monkeypatch.setenv(name, "something the user set for their own sessions")
    assert not any(name in job_env() for name in JOB_ENV_DROPPED)
    assert job_env()["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x")
    assert all(value is None for value in recorded()["config_env"].values())
