import json
import subprocess
import sys

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization import keystore
from gpu_token_utilization.api import BASE_URL, env_without_key, find_api_key
from gpu_token_utilization.cli import app
from gpu_token_utilization.jobs import _test_env

runner = CliRunner()
KEY = "bc_live_test_key_not_real"
CHAT = f"{BASE_URL}/chat/completions"
MODELS = {"data": [{"id": "qwen-3.8-27b"}]}
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)


def accepted():
    return respx.post(CHAT).mock(return_value=httpx.Response(200, json={"usage": {"total_tokens": 11}}))


# --- the store itself ---------------------------------------------------------------------

@windows_only
def test_credential_manager_round_trip(private_credential_target):
    assert keystore.target() == private_credential_target
    assert keystore.load() is None
    keystore.save(KEY)
    assert keystore.load() == KEY
    keystore.save("bc_live_replaced")
    assert keystore.load() == "bc_live_replaced"
    assert keystore.delete() is True
    assert keystore.load() is None and keystore.delete() is False


@windows_only
def test_environment_variable_wins_over_the_saved_key(monkeypatch):
    keystore.save("bc_live_saved")
    assert find_api_key() == ("bc_live_saved", "Windows Credential Manager (saved by gtu setkey)")
    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    assert find_api_key() == (KEY, "the BHARATCODE_API_KEY environment variable")


def test_no_key_anywhere():
    assert find_api_key() is None


# --- gtu setkey ---------------------------------------------------------------------------

@windows_only
@respx.mock
def test_setkey_checks_then_saves_and_says_where(monkeypatch):
    chat = accepted()
    result = runner.invoke(app, ["setkey"], input=KEY + "\n")
    assert result.exit_code == 0, result.output
    assert keystore.load() == KEY
    assert chat.call_count == 1 and chat.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(chat.calls.last.request.content)["max_tokens"] == 1
    out = result.output
    assert "BharatCode accepted the key" in out and "Windows Credential Manager" in out
    assert "every terminal" in out and "gtu setkey --remove" in out
    assert KEY not in out  # hidden input, never echoed
    assert "also set" not in out


@windows_only
@respx.mock
def test_setkey_does_not_save_a_rejected_key():
    respx.post(CHAT).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    result = runner.invoke(app, ["setkey"], input=KEY + "\n")
    assert result.exit_code == 1 and "rejected that key; nothing saved" in result.output
    assert keystore.load() is None


@windows_only
@respx.mock
def test_setkey_saves_anyway_when_bharatcode_is_busy():
    respx.post(CHAT).mock(return_value=httpx.Response(503))
    result = runner.invoke(app, ["setkey"], input=KEY + "\n")
    assert result.exit_code == 0 and "Couldn't check it right now" in result.output
    assert keystore.load() == KEY


@windows_only
@respx.mock
def test_setkey_no_check_sends_nothing():
    chat = accepted()
    result = runner.invoke(app, ["setkey", "--no-check"], input=KEY + "\n")
    assert result.exit_code == 0 and keystore.load() == KEY and chat.call_count == 0


@windows_only
@pytest.mark.parametrize("typed", ["", "   ", "bc_live two words"])
def test_setkey_refuses_empty_or_spaced_input(typed):
    result = runner.invoke(app, ["setkey", "--no-check"], input=typed + "\n")
    assert result.exit_code == 1 and "nothing saved" in result.output
    assert keystore.load() is None


@windows_only
def test_setkey_remove():
    keystore.save(KEY)
    result = runner.invoke(app, ["setkey", "--remove"])
    assert result.exit_code == 0 and "Removed the saved key" in result.output and keystore.load() is None
    again = runner.invoke(app, ["setkey", "--remove"])
    assert again.exit_code == 0 and "no saved key" in again.output


@windows_only
@respx.mock
def test_setkey_warns_that_the_environment_variable_takes_priority(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_from_env")
    accepted()
    result = runner.invoke(app, ["setkey"], input=KEY + "\n")
    assert result.exit_code == 0 and "also set in this terminal's environment" in result.output


@windows_only
def test_setkey_reads_a_piped_key_instead_of_hanging():
    # found on a clean Windows run: `"key" | gtu setkey` waited forever, because the hidden
    # prompt reads the console directly and ignores the pipe
    code = "import sys; from gpu_token_utilization.cli import app; sys.argv = ['gtu', 'setkey', '--no-check']; app()"
    proc = subprocess.run([sys.executable, "-c", code], input=KEY + "\n", capture_output=True,
                          text=True, timeout=60, env=env_without_key())
    assert proc.returncode == 0, proc.stderr
    assert keystore.load() == KEY and KEY not in proc.stdout + proc.stderr


@windows_only
def test_setkey_strips_the_byte_order_mark_powershell_pipes_add():
    # found on a clean Windows run: the piped key arrived as "\xef\xbb\xbf" + key, was saved
    # as is, and `gtu check` then crashed building the HTTP header
    code = "import sys; from gpu_token_utilization.cli import app; sys.argv = ['gtu', 'setkey', '--no-check']; app()"
    proc = subprocess.run([sys.executable, "-c", code], input=b"\xef\xbb\xbf" + KEY.encode() + b"\r\n",
                          capture_output=True, timeout=60, env=env_without_key())
    assert proc.returncode == 0, proc.stderr
    assert keystore.load() == KEY


@pytest.mark.parametrize("bad", ["bc_live_été", "bc_live_\x07bell"])
def test_unusable_key_in_the_environment_gives_a_message_not_a_traceback(bad, monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", bad)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1 and "isn't usable" in result.output and "gtu setkey" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_byte_order_mark_in_the_environment_is_ignored(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", "﻿" + KEY)
    assert find_api_key()[0] == KEY


# --- gtu check and the missing-key message -----------------------------------------------

@windows_only
@respx.mock
def test_check_uses_the_saved_key_and_says_so():
    keystore.save(KEY)
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=MODELS))
    chat = accepted()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "found in Windows Credential Manager" in result.output and "OK: API key accepted" in result.output
    assert chat.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


def assert_says_how_to_set_a_key(result):
    out = result.output
    assert result.exit_code == 1, out
    assert "no BharatCode API key found" in out and "https://bharatcode.ai/account" in out
    assert "gtu setkey" in out and '$env:BHARATCODE_API_KEY = "<your key>"' in out


@pytest.mark.parametrize("command", ["check", "demo"])
def test_missing_key_says_exactly_how_to_set_it(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert_says_how_to_set_a_key(runner.invoke(app, [command]))


def test_start_with_a_queued_job_and_no_key_says_how_to_set_it(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    assert runner.invoke(app, ["add", "review", "src"]).exit_code == 0
    assert_says_how_to_set_a_key(runner.invoke(app, ["start", "--now"]))


@respx.mock
def test_rejected_key_points_at_setkey(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=MODELS))
    respx.post(CHAT).mock(return_value=httpx.Response(401))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "rejected your API key, which came from the BHARATCODE_API_KEY environment variable" in result.output
    assert "gtu setkey" in result.output and KEY not in result.output


# --- subprocesses never see the key -------------------------------------------------------

def test_model_written_tests_and_the_cli_run_without_the_key(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", KEY)
    monkeypatch.setenv("PATH_MARKER", "kept")
    assert "BHARATCODE_API_KEY" not in env_without_key()
    assert env_without_key()["PATH_MARKER"] == "kept"
    env = _test_env()
    assert "BHARATCODE_API_KEY" not in env and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert KEY not in json.dumps(env)
