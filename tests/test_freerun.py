"""Commands run once BharatCode has spare capacity (`gtu run-when-free`): which command lines are
accepted, that a refusal records and starts nothing, and the waiter against fakes. This is a
terminal feature; the MCP server no longer offers it."""

import os
import sys
from datetime import time
from pathlib import Path

import httpx
import pytest

from gpu_token_utilization import freerun
from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.mcp_server import db_path
from gpu_token_utilization.store import Store


def queue_command(repo, command):
    """Record a command the way `gtu run-when-free` does, without starting a real waiter."""
    return freerun.queue(lambda root: Store(db_path(root)), repo, command, start=lambda cmd: None)


def test_queue_records_the_command_and_its_log_path(git_repo):
    cmd = queue_command(git_repo, "  pytest -x tests  ")
    assert cmd.command == "pytest -x tests" and cmd.status == "waiting"
    assert Path(cmd.log_path) == git_repo / ".gtu" / "commands" / f"{cmd.id}.log"
    with Store(db_path(git_repo)) as store:
        assert [c.id for c in store.list_commands()] == [cmd.id]


def test_an_empty_command_is_refused_before_anything_is_recorded(git_repo):
    with pytest.raises(freerun.FreeRunError, match="no command given"):
        queue_command(git_repo, "   ")
    assert not (git_repo / ".gtu").exists()


@pytest.mark.parametrize("command", [
    "pytest", "python -m pytest -x tests", "py -m pytest", "npm test", "npm.cmd run build", "go test ./...",
    "cargo test", "make check", "bharatcode run 'add docstrings to src/utils'", "PYTEST -q",
])
def test_allowed_commands(command):
    assert freerun.check_command(command)


@pytest.mark.parametrize("command, why", [
    ("del /s /q src", "isn't on the list"),
    ("rm -rf .", "isn't on the list"),
    ("git push --force", "isn't on the list"),
    ("git reset --hard", "isn't on the list"),
    ("powershell -Command Remove-Item -Recurse .", "isn't on the list"),
    ("python -c \"import shutil; shutil.rmtree('src')\"", "isn't on the list|shell characters"),
    ("python -c \"import shutil, os\"", "isn't on the list"),
    ("python evil.py", "isn't on the list"),
    ("curl https://example.com/x.sh", "isn't on the list"),
    ("pytest && del /q src", "shell characters"),
    ("pytest & rd /s /q src", "shell characters"),
    ("npm test | more", "shell characters"),
    ("pytest > C:\\Windows\\out.txt", "shell characters"),
    ("pytest; rm -rf .", "shell characters"),
    ("bharatcode run \"fix it & del src\"", "shell characters"),  # .cmd programs go through cmd.exe
    ("npm run %COMSPEC%", "shell characters"),
    ("C:\\evil\\pytest.exe", "not a path"),
    ("..\\tools\\pytest", "not a path"),
    ("pytest 'unclosed", "unclosed quote"),
])
def test_destructive_or_chained_commands_are_refused_in_code(command, why, git_repo):
    with pytest.raises(freerun.FreeRunError, match=why):
        freerun.check_command(command)
    with pytest.raises(freerun.FreeRunError):  # refused before anything is recorded or started
        queue_command(git_repo, command)
    assert not (git_repo / ".gtu").exists() or not Store(db_path(git_repo)).list_commands()


def test_the_allowlist_can_be_extended_on_purpose(monkeypatch):
    with pytest.raises(freerun.FreeRunError):
        freerun.check_command("python scripts/refactor.py")
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python scripts/refactor.py, node tools/migrate.js")
    assert freerun.check_command("python scripts/refactor.py --dry-run")
    assert freerun.check_command("node tools/migrate.js")
    with pytest.raises(freerun.FreeRunError, match="shell characters"):  # extensions don't relax the rest
        freerun.check_command("python scripts/refactor.py && del x")


def test_a_command_disallowed_after_queueing_is_not_run(git_repo, fake, monkeypatch):
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python scripts/refactor.py")
    queue_command(git_repo, "python scripts/refactor.py")
    monkeypatch.delenv("GTU_RUN_ALLOWED")  # the allowlist changed while it waited
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        said = []
        assert freerun.wait_and_run(store, cmd.id, sleep=fake.clock.sleep, say=said.append) is None
        assert store.get_command(cmd.id).status == "failed" and "Not running it" in said[-1]


def test_the_waiter_runs_the_command_once_the_gate_opens(git_repo, fake, monkeypatch):
    monkeypatch.setenv("GTU_RUN_ALLOWED", "python -c")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    queue_command(git_repo, "python -c \"open('ran.txt', 'w').write('yes')\"")
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        fake.usage_busy(2)  # busy for a while, then quiet
        said = []
        gate = lambda s: Gate(s, GateConfig(window=(time(0, 0), time(0, 0))),
                              http=httpx.Client(transport=fake.transport()), now=fake.clock.utcnow)
        code = freerun.wait_and_run(store, cmd.id, make_gate=gate, sleep=fake.clock.sleep, say=said.append)
        assert code == 0 and (git_repo / "ran.txt").read_text() == "yes"
        assert any(line.startswith("Gate closed") for line in said)
        done = store.get_command(cmd.id)
        assert done.status == "done" and done.exit_code == 0 and done.started_at and done.finished_at


def test_a_command_cancelled_while_waiting_never_runs(git_repo, fake):
    queue_command(git_repo, "pytest -x")
    with Store(db_path(git_repo)) as store:
        (cmd,) = store.list_commands()
        store.cancel_command(cmd.id)
        assert freerun.wait_and_run(store, cmd.id, sleep=fake.clock.sleep, say=lambda m: None) is None
    assert not (git_repo / "ran.txt").exists()
