"""Queueing and cancelling jobs: the code behind `gtu add` / `gtu cancel` and the matching MCP
tools, so both front ends behave identically."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import runstate, worktree
from .store import Job, Store, StoreError


class ActionError(Exception):
    """The request was refused; the message says why and what to do, for the user."""


@dataclass
class Outcome:
    job: Job
    note: str | None = None  # something the user should know, e.g. a big folder to copy


def queue_job(open_store: Callable[[Path], Store], base: Path, kind: str, target: str,
              note: str | None = None) -> Outcome:
    """Queue a job on `target` (relative to `base`, or absolute) in the project containing it (its git repo, if it's in one, else `base`).
    The store is only opened (and so only created) once the request has passed every check;
    `open_store` gets the repo root."""
    try:
        repo = worktree.repo_root(base).resolve()
        worktree.ensure_clean(repo)
    except worktree.GitError as exc:
        raise ActionError(str(exc)) from None
    path = (Path(base) / target).resolve()
    if not path.exists():
        raise ActionError(f"{target} does not exist (looked in {base})")
    try:
        relative = path.relative_to(repo)
    except ValueError:
        raise ActionError(f"{target} is outside this repo ({repo})") from None
    try:
        with open_store(repo) as store:
            job = store.add_job(kind, relative.as_posix() or ".", str(repo), note)
    except StoreError as exc:
        raise ActionError(str(exc)) from None
    notes = []
    size = worktree.size_warning(repo)  # a plain folder whose copy would be big: say so now, not at 3 am
    if size:
        notes.append(size)
    return Outcome(job, "\n".join(notes) or None)


def stop_runner(store: Store, wait: float = 10.0) -> list[str]:
    """Stop a running `gtu start`: ask first, kill after `wait` seconds. Jobs are paused, not
    failed. Returns what happened, line by line, for the user."""
    folder = store.path.parent
    lines = []
    pid = runstate.running_pid(folder)
    if pid is None:
        runstate.clear(folder)
        paused = runstate.tidy(store)  # e.g. after a crash: don't leave jobs marked running
        return ["Nothing is running." + (f" Paused interrupted job(s): {', '.join(paused)}." if paused else "")]
    runstate.request_stop(folder)
    lines.append(f"Asked `gtu start` (process {pid}) to stop...")
    if runstate.wait_for_exit(pid, wait):
        lines.append("It stopped cleanly.")
    elif runstate.kill_tree(pid):
        lines.append(f"It didn't stop within {wait:g} s, so it was killed. A Desktop session it had started may still be finishing in Desktop; stop it there if you need to.")
    else:
        raise ActionError(f"process {pid} doesn't look like gtu, so it was not killed; stop it yourself.")
    runstate.clear(folder)
    paused = runstate.tidy(store)
    held = [j.id for j in store.list_jobs(include_finished=False) if j.status == "paused"]
    if paused or held:
        lines.append(f"Paused job(s): {', '.join(sorted(set(paused + held)))}. "
                     "Unfinished edits were discarded; finished steps are kept on each job's branch.")
    return lines


def cancel_job(store: Store, job_id: str) -> Outcome:
    """Cancel a job that hasn't finished and remove its worktree; its branch is kept."""
    try:
        job = store.cancel_job(job_id)
    except StoreError as exc:
        raise ActionError(str(exc)) from None
    warning = None
    try:
        worktree.remove(Path(job.repo), job.id)
    except worktree.GitError as exc:
        warning = f"could not remove the job's worktree: {exc}"
    return Outcome(job, warning)
