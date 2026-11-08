"""Coordination between a running `gtu start` and `gtu stop`, in the store's folder (.gtu/).

run.json  written by `gtu start` while it runs: its process id.
stop      created by `gtu stop`; `gtu start` checks for it about once a second and stops cleanly.

If a clean stop doesn't happen in time (e.g. stuck in a long request), `gtu stop` kills the
process tree, then `tidy` marks interrupted jobs paused and drops their unfinished edits.
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _looks_like_gtu(pid: int) -> bool:
    """Guard against a reused pid before killing anything."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        name = out.split(",")[0].strip('"').lower() if out.startswith('"') else ""
    else:
        try:
            name = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").lower()
        except OSError:
            name = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.lower()
    return any(word in name for word in ("python", "gtu"))


def wait_for_exit(pid: int, seconds: float, poll: float = 0.2) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(poll)
    return not alive(pid)


def kill_tree(pid: int) -> bool:
    """Kill the process and its children. False if it doesn't look like gtu."""
    if not _looks_like_gtu(pid):
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal

        # Not killpg on gtu's own group: that may be the user's shell. A child may run in its own
        # session, so kill each child's group, then gtu itself.
        children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
        for child in children:
            try:
                os.killpg(int(child), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return wait_for_exit(pid, 5)


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
