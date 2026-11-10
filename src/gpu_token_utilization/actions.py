"""Queueing and cancelling jobs: what the MCP tools spare_delegate and spare_cancel_work do."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import worktree
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
    stored = relative.as_posix() or "."
    try:
        with open_store(repo) as store:
            job = store.add_job(kind, stored, str(repo), note)
    except StoreError as exc:
        raise ActionError(str(exc)) from None
    notes = []
    size = worktree.size_warning(repo)  # a plain folder whose copy would be big: say so now, not at 3 am
    if size:
        notes.append(size)
    return Outcome(job, "\n".join(notes) or None)


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
