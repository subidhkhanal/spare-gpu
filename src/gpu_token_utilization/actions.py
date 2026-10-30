"""Queueing and cancelling jobs: the code behind `gtu add` / `gtu cancel` and the matching MCP
tools, so both front ends behave identically."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import worktree
from .agent_cli import CLI_KINDS, CliNotFound, resolve_command
from .store import Job, Store, StoreError


class ActionError(Exception):
    """The request was refused; the message says why and what to do, for the user."""


@dataclass
class Outcome:
    job: Job
    note: str | None = None  # something the user should know, e.g. the CLI is missing


def queue_job(open_store: Callable[[Path], Store], base: Path, kind: str, target: str,
              note: str | None = None) -> Outcome:
    """Queue a job on `target` (relative to `base`, or absolute) in the git repo containing it.
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
    warning = None
    if job.kind in CLI_KINDS:
        try:
            resolve_command()
        except CliNotFound as exc:
            warning = f"This job won't run yet. {exc}\nIt stays queued and runs on the first `gtu start` after that."
    return Outcome(job, warning)


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
