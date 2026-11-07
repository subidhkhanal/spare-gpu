"""Coordination between whoever is running jobs and `gtu stop`, in the store's folder (.gtu/).

run.json  written while jobs run (by `gtu start`, or by the MCP server while a delegated job
          runs in front of the user): the process id, so a second runner doesn't start.
stop      created by `gtu stop` (or spare_cancel_work for one job); the runner checks for it
          between steps and every few seconds inside a step, and stops cleanly, leaving the
          job paused and resumable. Nothing is ever killed.

`tidy` makes jobs left 'running' by a crashed process resumable.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import worktree
from .store import Store

RUN_FILE = "run.json"
STOP_FILE = "stop"


class StopRequested(Exception):
    """`gtu stop` asked the running `gtu start` to stop."""


def write_run(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / RUN_FILE).write_text(json.dumps({"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat()}))


def read_run(folder: Path) -> dict | None:
    try:
        return json.loads((folder / RUN_FILE).read_text())
    except (OSError, ValueError):
        return None


def clear(folder: Path) -> None:
    (folder / RUN_FILE).unlink(missing_ok=True)
    (folder / STOP_FILE).unlink(missing_ok=True)


def request_stop(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / STOP_FILE).write_text(datetime.now(timezone.utc).isoformat())


def stop_requested(folder: Path) -> bool:
    return (folder / STOP_FILE).exists()


def alive(pid: int) -> bool:
    if os.name == "nt":
        # Not os.kill(pid, 0): on Windows that terminates the process.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(folder: Path) -> int | None:
    """The pid of a `gtu start` that is still running, or None (a stale run file doesn't count)."""
    info = read_run(folder)
    pid = info.get("pid") if info else None
    return pid if isinstance(pid, int) and alive(pid) else None


def wait_until_stopped(folder: Path, seconds: float, poll: float = 0.2) -> bool:
    """True once no run is going in `folder` (the runner cleared its run file, or its process is gone)."""
    deadline = time.monotonic() + seconds
    while running_pid(folder) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def tidy(store: Store) -> list[str]:
    """Make interrupted jobs resumable: running steps back to pending, their unfinished edits
    dropped from the job's own worktree, and the job paused (not failed). Returns the job ids."""
    paused = []
    for job in store.list_jobs(include_finished=False):
        if job.status != "running" and not any(s["status"] == "running" for s in store.steps(job.id)):
            continue
        store.reset_interrupted_steps(job.id)
        path = worktree.worktree_path(Path(job.repo), job.id)
        if (path / ".git").exists():
            try:
                worktree.discard_uncommitted(path)
            except worktree.GitError:
                pass  # resume discards it again before rerunning the step
        store.set_status(job.id, "paused")
        paused.append(job.id)
    return paused
