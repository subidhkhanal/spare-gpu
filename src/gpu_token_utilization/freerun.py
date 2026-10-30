"""Run any command once BharatCode has spare capacity (`spare_run_when_free`, `gtu run-when-free`).

Queueing records the command and starts a small detached waiter, so the caller (often an agent)
never blocks. The waiter asks the capacity gate, as `gtu start` does, until it opens, then runs
the command in the repo with Command Prompt (cmd.exe), without our API key in its environment,
and records the exit code. Output goes to .gtu/commands/<id>.log.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import runner, runstate, worktree
from .api import env_without_key
from .gate import Gate, GateConfig
from .store import Command, Store

CHECK_EVERY_S = 60.0  # never faster than the live stats are sampled


class FreeRunError(Exception):
    """The command wasn't queued; the message says why."""


def log_path(repo: Path, command_id: str) -> Path:
    return Path(repo) / ".gtu" / "commands" / f"{command_id}.log"


def queue(open_store: Callable[[Path], Store], base: Path, command: str,
          start: Callable[[Command], int] | None = None) -> Command:
    """Record `command` for the repo containing `base` and start its waiter."""
    command = command.strip()
    if not command:
        raise FreeRunError("no command given")
    try:
        repo = worktree.repo_root(base).resolve()
    except worktree.GitError:
        repo = Path(base).resolve()
    with open_store(repo) as store:
        cmd = store.add_command(command, str(repo))
        log = log_path(repo, cmd.id)
        store.update_command(cmd.id, log_path=str(log))
        pid = (start or _start_waiter)(store.get_command(cmd.id))
        return store.update_command(cmd.id, pid=pid)


def _start_waiter(cmd: Command) -> int:
    args = [sys.executable, "-m", "gpu_token_utilization", "_run-when-free", cmd.id]
    return runner.spawn(args, Path(cmd.repo), Path(cmd.log_path)).pid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wait_and_run(store: Store, command_id: str, make_gate: Callable[[Store], Gate] | None = None,
                 sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> int | None:
    """Wait for the gate to open, then run the command. Returns its exit code, or None if it was
    cancelled first."""
    gate = (make_gate or (lambda s: Gate(s, GateConfig.from_env())))(store)
    try:
        while True:
            cmd = store.get_command(command_id)
            if cmd.status == "cancelled":
                say("Cancelled before it ran.")
                return None
            decision = gate.check(allow_probe=False)
            if decision.open:
                break
            say(decision.describe())
            sleep(max(CHECK_EVERY_S, decision.next_check))
    finally:
        gate.close()

    say(f"Capacity is free. Running: {cmd.command}")
    store.update_command(command_id, status="running", started_at=_now())
    Path(cmd.log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cmd.log_path, "ab") as log:
        # cmd.pid stays the waiter's: cancelling kills the waiter's process tree, command included.
        code = subprocess.run(cmd.command, shell=True, cwd=cmd.repo, stdin=subprocess.DEVNULL,
                              stdout=log, stderr=subprocess.STDOUT, env=env_without_key()).returncode
    store.update_command(command_id, status="done" if code == 0 else "failed", exit_code=code, finished_at=_now())
    say(f"Finished with exit code {code}.")
    return code


def cancel(store: Store, command_id: str) -> Command:
    """Cancel a waiting command, or stop a running one (its waiter and everything under it)."""
    cmd = store.cancel_command(command_id)
    if cmd.pid and runstate.alive(cmd.pid):
        runstate.kill_tree(cmd.pid)
    return cmd
