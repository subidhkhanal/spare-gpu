"""Start `gtu start` in the background, detached from whoever asked for it.

The MCP server lives only as long as the BharatCode session that started it, but queued jobs
are meant to run overnight. So the runner is a separate process: no window, its own process
group, out of BharatCode's job object where Windows allows it, output in .gtu/runner.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import runstate

LOG_NAME = "runner.log"
CREATE_BREAKAWAY_FROM_JOB = 0x01000000  # survive the parent app closing, if its job object allows


class RunnerError(Exception):
    """The runner didn't start; the message says why."""


def command() -> list[str]:
    return [sys.executable, "-m", "gpu_token_utilization", "start"]


def _popen(args: list[str], repo: Path, log) -> subprocess.Popen:
    common = dict(cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    if os.name != "nt":
        return subprocess.Popen(args, start_new_session=True, **common)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        # The parent's job object forbids breakaway; started anyway, it may end when the parent does.
        return subprocess.Popen(args, creationflags=flags, **common)


def launch(repo: Path, args: list[str] | None = None, wait: float = 15.0) -> int:
    """Start the runner for `repo` and return its pid once it has registered itself."""
    repo = Path(repo)
    folder = repo / ".gtu"
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / LOG_NAME
    with open(log_path, "ab") as log:
        log.write(f"\n--- started by spare-gpu at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        log.flush()
        proc = _popen(args or command(), repo, log)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        # The pid that registers can differ from proc.pid: a venv's python.exe on Windows is a
        # launcher that starts the real interpreter as its child.
        pid = runstate.running_pid(folder)
        if pid:
            return pid
        if proc.poll() is not None:
            if proc.returncode == 0:
                return proc.pid  # already done (nothing that could run right now)
            tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
            raise RunnerError("the runner stopped straight away:\n" + "\n".join(tail))
        time.sleep(0.2)
    return proc.pid  # still starting up (slow disk or antivirus); it registers itself shortly
