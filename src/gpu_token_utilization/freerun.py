"""Run a test, build or lint command once BharatCode has spare capacity (`gtu run-when-free`).

Queueing records the command and starts a small detached waiter, so the caller (often an agent)
never blocks. The waiter asks the capacity gate, as `gtu start` does, until it opens, then runs
the command in the repo, without our API key in its environment, and records the exit code.
Output goes to .gtu/commands/<id>.log.

Safety is enforced here, not by asking the agent: only allowlisted programs run, never through a
shell, and shell characters are refused, so an allowed command can't chain a destructive one.
"""

from __future__ import annotations

import os
import shutil
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
ALLOWED_ENV = "GTU_RUN_ALLOWED"  # extra allowed command prefixes, comma-separated

# Script runners and make name their scripts and targets exactly: a bare `npm run` or `make`
# would run whatever the project defines, deploy, publish and clean included.
SCRIPTS = ("test", "build", "lint", "check", "typecheck")
# What may run unattended, enforced here rather than left to the agent's judgement: test, build
# and lint tools only. Each entry is a command prefix; the program is matched by name (any
# folder, .exe/.cmd/.bat), the rest word for word, and arguments after the prefix are passed on
# (`npm run build -- --watch`).
ALLOWED = (
    "pytest", "python -m pytest", "py -m pytest", "python -m unittest", "py -m unittest",
    "python -m mypy", "python -m ruff", "ruff", "mypy", "flake8", "tox",
    "npm test", "pnpm test", "yarn test", "npx tsc", "tsc", "eslint",
    *(f"{runner} run {script}" for runner in ("npm", "pnpm", "yarn") for script in SCRIPTS),
    "go test", "go build", "go vet", "cargo test", "cargo build", "cargo check", "cargo clippy",
    "dotnet test", "dotnet build", "mvn test", "mvn verify", "gradle test", "gradlew test",
    *(f"make {target}" for target in SCRIPTS),
)
# BharatCode's own CLI is never started from here: a `bharatcode run` queued this way would work
# in the user's real checkout, with their full config and none of a job's permissions. Agent
# work goes through jobs (spare_delegate in a chat, `gtu add` in a terminal), which run in a
# private copy under a permission file. Refused by program name, so GTU_RUN_ALLOWED can't
# bring it back, and a command queued before this rule is refused when it comes up to run.
NEVER = ("bharatcode", "opencode")
# Characters cmd.exe acts on. Commands never go through a shell, but npm and other .cmd/.bat
# programs are run by cmd.exe whatever we do, so these are refused outright.
SHELL_CHARACTERS = set("&|<>^%;`\n\r")


class FreeRunError(Exception):
    """The command wasn't queued (or wasn't run); the message says why."""


def _words(text: str) -> list[str]:
    """Split a command line the way a user types it: quotes group, backslashes are literal
    (Windows paths)."""
    words, current, quote = [], [], None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(ch)
    if quote:
        raise FreeRunError("the command has an unclosed quote")
    if current:
        words.append("".join(current))
    return words


def _program(word: str) -> str:
    name = Path(word).name.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def allowed_prefixes() -> list[list[str]]:
    extra = [p.strip() for p in os.environ.get(ALLOWED_ENV, "").split(",") if p.strip()]
    return [_words(p) for p in (*ALLOWED, *extra)]


def check_command(command: str) -> list[str]:
    """The command as program + arguments if it may run unattended; FreeRunError otherwise."""
    command = command.strip()
    if not command:
        raise FreeRunError("no command given")
    bad = sorted(SHELL_CHARACTERS & set(command))
    if bad:
        shown = " ".join(repr(c) if c in "\n\r" else c for c in bad)
        raise FreeRunError(f"commands can't contain shell characters ({shown}); give one plain command, "
                           "without &&, pipes, redirects or variables")
    words = _words(command)
    if any(sep in words[0] for sep in ("/", "\\", ":")):
        raise FreeRunError(f"give the program by name (e.g. `pytest`), not a path like `{words[0]}`; "
                           "it's looked up on PATH")
    if _program(words[0]) in NEVER:
        raise FreeRunError(f"spare-gpu doesn't start BharatCode's CLI from here: `{words[0]}` would work in your "
                           "real checkout with your full config. Hand agent work to a job instead (spare_delegate "
                           "in a chat, or `gtu add` in a terminal)")
    for prefix in allowed_prefixes():
        if len(words) >= len(prefix) and _program(words[0]) == _program(prefix[0]) \
                and [w.lower() for w in words[1:len(prefix)]] == [p.lower() for p in prefix[1:]]:
            return words
    raise FreeRunError(
        f"`{command}` isn't on the list of commands spare-gpu runs unattended. Allowed: test, build and lint "
        f"tools ({', '.join(ALLOWED[:6])}, npm test, npm run {'/'.join(SCRIPTS)}, go test, cargo test, "
        f"make {'/'.join(SCRIPTS)}, ...). To allow more, set {ALLOWED_ENV} (comma-separated command prefixes)"
    )


def _argv(words: list[str]) -> list[str]:
    """The program to start, from PATH."""
    found = shutil.which(words[0])
    if not found:
        raise FreeRunError(f"`{words[0]}` wasn't found on PATH")
    return [found, *words[1:]]


def log_path(repo: Path, command_id: str) -> Path:
    return Path(repo) / ".gtu" / "commands" / f"{command_id}.log"


def queue(open_store: Callable[[Path], Store], base: Path, command: str,
          start: Callable[[Command], int] | None = None) -> Command:
    """Record `command` for the repo containing `base` and start its waiter."""
    command = command.strip()
    check_command(command)  # refused before anything is recorded or started
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
    cancelled or refused first."""
    if not _still_allowed(store, command_id, say):
        return None  # no point waiting for capacity to run something we won't run
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

    argv = _still_allowed(store, command_id, say)  # again: the allowlist may have changed while it waited
    if not argv:
        return None
    say(f"Capacity is free. Running: {cmd.command}")
    store.update_command(command_id, status="running", started_at=_now())
    Path(cmd.log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cmd.log_path, "ab") as log:
        # No shell: the command can't chain or redirect. cmd.pid stays the waiter's, so cancelling
        # kills the waiter's process tree, command included.
        code = subprocess.run(argv, cwd=cmd.repo, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, env=env_without_key()).returncode
    store.update_command(command_id, status="done" if code == 0 else "failed", exit_code=code, finished_at=_now())
    say(f"Finished with exit code {code}.")
    return code


def _still_allowed(store: Store, command_id: str, say: Callable[[str], None]) -> list[str] | None:
    """The argv to run, or None after marking the command failed because it may not run."""
    cmd = store.get_command(command_id)
    if cmd.status == "cancelled":
        say("Cancelled before it ran.")
        return None
    try:
        return _argv(check_command(cmd.command))
    except FreeRunError as exc:  # the allowlist (or this rule set) changed since it was queued
        say(f"Not running it: {exc}")
        store.update_command(command_id, status="failed", finished_at=_now())
        return None


def cancel(store: Store, command_id: str) -> Command:
    """Cancel a waiting command, or stop a running one (its waiter and everything under it)."""
    cmd = store.cancel_command(command_id)
    if cmd.pid and runstate.alive(cmd.pid):
        runstate.kill_tree(cmd.pid)
    return cmd
