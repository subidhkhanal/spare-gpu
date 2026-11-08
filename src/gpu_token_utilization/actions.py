"""Queueing and cancelling jobs: the code behind `gtu add` / `gtu cancel` and the matching MCP
tools, so both front ends behave identically."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import runstate, worktree
from .jobs import plan_file_problem
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
    if kind == "implement":  # its target is the plan to carry out, not code to work on
        problem = plan_file_problem(path, target)
        if problem:
            raise ActionError(problem)
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
    """Ask the running jobs to stop and wait up to `wait` seconds for it. Nothing is killed: the
    runner notices between steps and every few seconds inside one, aborts the current Desktop
    session and leaves the job paused, not failed. Returns what happened, line by line."""
    folder = store.path.parent
    lines = []
    pid = runstate.running_pid(folder)
    if pid is None:
        runstate.clear(folder)
        paused = runstate.tidy(store)  # e.g. after a crash: don't leave jobs marked running
        return ["Nothing is running." + (f" Paused interrupted job(s): {', '.join(paused)}." if paused else "")]
    runstate.request_stop(folder)
    lines.append(f"Asked the running job (process {pid}) to stop...")
    if runstate.wait_until_stopped(folder, wait):
        lines.append("It stopped cleanly.")
        runstate.clear(folder)
        paused = runstate.tidy(store)
    else:
        lines.append(f"It hasn't stopped within {wait:g} s; it stops at its next check, within a few seconds of "
                     "the current request finishing. The stop request stays in place.")
        paused = []
    held = [j.id for j in store.list_jobs(include_finished=False) if j.status == "paused"]
    if paused or held:
        lines.append(f"Paused job(s): {', '.join(sorted(set(paused + held)))}. "
                     "Unfinished edits were discarded; finished steps are kept in each job's copy.")
    return lines


def cancel_job(store: Store, job_id: str) -> Outcome:
    """Cancel a job that hasn't finished. A queued or paused job's copy is removed here; a running
    job's copy is removed by its runner, which notices the cancellation within a few seconds,
    aborts the step and cleans up."""
    try:
        was_running = store.get_job(job_id).status == "running"
        job = store.cancel_job(job_id)
    except StoreError as exc:
        raise ActionError(str(exc)) from None
    if was_running:
        return Outcome(job, "it was running; it stops within a few seconds")
    warning = None
    try:
        worktree.remove(Path(job.repo), job.id)
    except worktree.GitError as exc:
        warning = f"could not remove the job's worktree: {exc}"
    return Outcome(job, warning)
