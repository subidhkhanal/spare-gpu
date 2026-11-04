import json
import sys
import time

import pytest

from gpu_token_utilization import agent_cli
from gpu_token_utilization.agent_cli import BharatCodeCli, CliNotFound, permission_config

FAKE_CLI = [sys.executable, "-m", "gpu_token_utilization.fakecli"]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    path = tmp_path / "scenario.json"
    record = tmp_path / "calls.jsonl"
    monkeypatch.setenv("GTU_FAKE_CLI_SCENARIO", str(path))

    def write(**data):
        data.setdefault("record", str(record))
        path.write_text(json.dumps(data))
        return record

    return write


def test_reports_tokens_text_and_tools(tmp_path, scenario):
    scenario(steps=[{"tokens": 11500, "writes": {"a.py": "x = 1\n"}}, {"tokens": 300}], text="created a.py")
    seen = []
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "do it", on_step=seen.append)
    assert result.ok and result.exit_code == 0
    assert result.tokens == 11800 and [s.total_tokens for s in seen] == [11500, 300]
    assert result.text == "created a.py"
    assert result.tools[0].startswith("edit ") and (tmp_path / "a.py").exists()


def test_runs_in_the_given_dir_and_passes_the_prompt_untouched(tmp_path, scenario):
    record = scenario()
    prompt = 'Write tests & docs | for "x" > y %PATH% $(rm -rf /)'
    BharatCodeCli(command=FAKE_CLI, model="bharatcode/qwen-3.8-27b").run(tmp_path, prompt)
    args = json.loads(record.read_text().splitlines()[0])["args"]
    assert args[0] == "run"
    assert "--pure" not in args  # it drops BharatCode's sign-in plugin; later steps then fail
    assert args[args.index("--dir") + 1] == str(tmp_path)
    assert args[args.index("-m") + 1] == "bharatcode/qwen-3.8-27b"
    assert args[args.index("--format") + 1] == "json"
    assert args[-1] == prompt


def test_writes_a_deny_first_allowlist_and_removes_it_afterwards(tmp_path, scenario):
    record = scenario()
    BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x", commands=("python -m pytest", "pytest"))
    config = json.loads(record.read_text().splitlines()[0])["config"]
    bash = config["permission"]["bash"]
    assert list(bash) == ["*", "python -m pytest*", "pytest*"]  # deny first: last match wins
    assert bash["*"] == "deny" and bash["pytest*"] == "allow"
    assert config["permission"]["webfetch"] == "deny"
    assert config["permission"]["skill"] == "deny"  # and with it the skill list, ~3.5k tokens a step
    assert not (tmp_path / "opencode.json").exists()


def test_no_commands_means_no_shell_at_all():
    perm = permission_config(edit=True, commands=())["permission"]
    assert perm["bash"] == "deny" and perm["edit"] == "allow"
    assert permission_config(edit=False, commands=())["permission"]["edit"] == "deny"


def test_jobs_switch_the_users_external_skills_off(tmp_path, scenario):
    # measured: 16 Claude Code skills from ~/.claude/skills add ~3.5k tokens to every CLI step
    record = scenario()
    BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x")
    assert json.loads(record.read_text().splitlines()[0])["skills_off"] is True


def test_jobs_switch_spare_gpus_own_mcp_server_off():
    # measured: its tool schemas add ~2.3k tokens to every CLI step, and a job shouldn't queue jobs
    assert permission_config(edit=True, commands=())["mcp"]["spare-gpu"]["enabled"] is False


def test_killed_when_the_token_budget_is_reached(tmp_path, scenario):
    scenario(steps=[{"tokens": 5000, "sleep": 0.3} for _ in range(10)])
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x", token_budget=12000)
    assert result.stopped == "budget" and not result.ok
    assert 12000 <= result.tokens <= 15000  # stopped right after the step that crossed it


def test_killed_on_timeout(tmp_path, scenario):
    scenario(steps=[{"tokens": 100, "sleep": 30}])
    started = time.monotonic()
    result = BharatCodeCli(command=FAKE_CLI, timeout=2).run(tmp_path, "x")
    assert result.stopped == "timeout"
    assert time.monotonic() - started < 15


def test_failure_is_reported_with_its_message(tmp_path, scenario):
    scenario(error="Unexpected server error", exit=1)
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "x")
    assert not result.ok and result.exit_code == 1
    assert result.error == "Unexpected server error"


def test_command_override_from_env(monkeypatch):
    monkeypatch.setenv("GTU_BHARATCODE_CMD", '["node", "C:/x/bharatcode.mjs"]')
    assert agent_cli.resolve_command() == ["node", "C:/x/bharatcode.mjs"]


def test_npm_cmd_shim_is_bypassed_so_the_shell_never_sees_the_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("GTU_BHARATCODE_CMD", raising=False)
    shim = tmp_path / "bharatcode.cmd"
    shim.write_text("@echo off")
    script = tmp_path / "node_modules" / "bharatcode" / "bin" / "bharatcode.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: str(shim) if name == "bharatcode" else "C:/node.exe")
    assert agent_cli.resolve_command() == ["C:/node.exe", str(script)]


def test_missing_cli_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GTU_BHARATCODE_CMD", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    with pytest.raises(CliNotFound, match="npm install -g bharatcode"):
        agent_cli.resolve_command()


CRED = agent_cli.CREDENTIAL_ERROR + "."


def calls(record):
    return [json.loads(line) for line in record.read_text().splitlines()]


def test_a_credential_error_continues_the_same_session_and_keeps_the_work(tmp_path, scenario):
    record = scenario(steps=[{"tokens": 1000}], error=CRED, fail_first=1, text="all done")
    retried = []
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "write tests", on_retry=retried.append)
    assert result.ok and result.retries == 1 and retried == [1]
    assert result.tokens == 2000 and result.text == "all done"  # steps from both calls count
    first, second = calls(record)
    assert "--session" not in first["args"] and first["args"][-1] == "write tests"
    assert second["args"][second["args"].index("--session") + 1] == "ses_fake"
    assert second["args"][-1] == agent_cli.CONTINUE_PROMPT
    assert second["config"] == first["config"]  # the permission rules still apply on the retry
    assert not (tmp_path / agent_cli.CONFIG_FILE).exists()


def test_a_credential_error_that_keeps_happening_fails_after_the_last_retry(tmp_path, scenario):
    record = scenario(steps=[{"tokens": 500}], error=CRED)
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "write tests")
    assert not result.ok and agent_cli.CREDENTIAL_ERROR in result.error
    assert result.retries == agent_cli.CREDENTIAL_RETRIES and len(calls(record)) == agent_cli.CREDENTIAL_RETRIES + 1


def test_other_errors_are_not_retried(tmp_path, scenario):
    record = scenario(steps=[{"tokens": 500}], error="model overloaded", fail_first=1)
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "write tests")
    assert not result.ok and result.error == "model overloaded" and result.retries == 0
    assert len(calls(record)) == 1


def test_the_token_budget_covers_the_retries_too(tmp_path, scenario):
    scenario(steps=[{"tokens": 1000}], error=CRED, fail_first=1)
    result = BharatCodeCli(command=FAKE_CLI).run(tmp_path, "write tests", token_budget=1500)
    assert result.stopped == "budget" and result.tokens == 2000
