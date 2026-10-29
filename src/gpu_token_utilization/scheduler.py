"""Runs queued jobs one step at a time, only while the capacity gate is open.

Each job works in its own git worktree on branch gtu/<job-id>; the user's checkout
is never modified. Progress is saved after every step: the step is committed on the
job's branch and marked done. A step left 'running' by a killed process never
finished, so on restart its partial edits are discarded and it runs again; finished
steps never do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import worktree
from .client import AuthError, BharatCodeClient, BharatCodeError, BusyError, ChatResult
from .gate import Decision, Gate
from .store import JOB_KINDS, Job, Store

# Files jobs may create for their own tooling that must never be committed.
NEVER_COMMIT = ("opencode.json",)


@dataclass
class StepContext:
    job: Job
    step_id: int
    step_name: str
    store: Store
    client: BharatCodeClient
    decision: Decision
    workdir: Path  # the job's worktree (or the repo itself when isolation is off, in tests)

    def chat(self, messages: list[dict], **kwargs) -> ChatResult:
        return self.client.chat(
            messages, job_id=self.job.id, step_id=self.step_id, gate=self.decision.gate_info, **kwargs
        )


StepFn = Callable[[StepContext], str]
Plan = list[tuple[str, StepFn]]

MAX_SURVEY_CHARS = 12_000
MAX_FILE_BYTES = 200_000
SKIP_DIRS = {".git", ".gtu", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}

SURVEY_ASKS = {
    "tests": "List the functions or behaviours in this code that most need tests, and the cases each test should cover.",
    "docs": "List what documentation is missing or unclear in this code: docstrings, comments and README sections.",
    "review": "List the riskiest spots, likely bugs and concrete suggested fixes in this code.",
}


def _read_target(target: Path) -> str:
    """The target's text, capped. For a folder: its file list, then file contents until the cap."""
    if target.is_file():
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_SURVEY_CHARS]
    files = sorted(
        p for p in target.rglob("*")
        if p.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(target).parts)
    )
    parts = ["Files:\n" + "\n".join(p.relative_to(target).as_posix() for p in files[:200])]
    budget = MAX_SURVEY_CHARS - len(parts[0])
    for path in files:
        if budget <= 0 or path.stat().st_size > MAX_FILE_BYTES:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        chunk = f"\n--- {path.relative_to(target).as_posix()} ---\n" + data.decode("utf-8", errors="replace")
        parts.append(chunk[:budget])
        budget -= len(chunk)
    return "".join(parts)


def survey_step(ctx: StepContext) -> str:
    """Phase 2 placeholder work: ask for a short plan of what the job should do. Phase 3 replaces this."""
    job = ctx.job
    text = _read_target(ctx.workdir / job.target)
    ask = SURVEY_ASKS[job.kind] + " At most 8 bullet points."
    if job.note:
        ask += f" The developer adds: {job.note}"
    result = ctx.chat(
        [
            {"role": "system", "content": "You help a developer with work on their own repository. Be concise."},
            {"role": "user", "content": f"{ask}\n\nTarget: {job.target}\n\n{text}"},
        ],
        max_tokens=800,
        reasoning_effort="none",
    )
    return result.content + ("\n\n[cut off at the output token limit]" if result.truncated else "")


DEFAULT_PLANS: dict[str, Plan] = {kind: [("survey", survey_step)] for kind in JOB_KINDS}

_STATUS_ORDER = {"running": 0, "paused": 1, "queued": 2}


class Scheduler:
    def __init__(
        self,
        store: Store,
        gate: Gate,
        client: BharatCodeClient,
        *,
        plans: dict[str, Plan] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda message: None,
        isolate: bool = True,
    ):
        self.store = store
        self.gate = gate
        self.client = client
        self.plans = plans or DEFAULT_PLANS
        self._sleep = sleep
        self._log = log
        self.isolate = isolate

    def pending_jobs(self) -> list[Job]:
        jobs = self.store.list_jobs(include_finished=False)
        return sorted(jobs, key=lambda j: _STATUS_ORDER.get(j.status, 3))

    def run(self, stop: Callable[[], bool] = lambda: False, max_steps: int | None = None) -> int:
        """Work through the queue until it is empty. Returns the number of steps completed."""
        completed = 0
        while not stop():
            jobs = self.pending_jobs()
            if not jobs:
                self._log("Queue empty.")
                return completed
            job = jobs[0]
            decision = self.gate.check()
            if not decision.open:
                if job.status == "running":
                    self.store.set_status(job.id, "paused")
                self._log(decision.describe())
                self._sleep(decision.next_check)
                continue
            if self.run_step(job, decision) == "done":
                completed += 1
                if max_steps is not None and completed >= max_steps:
                    return completed
        return completed

    def run_step(self, job: Job, decision: Decision) -> str:
        """Run the job's next unfinished step. Returns done, busy, failed or finished."""
        plan = self.plans[job.kind]
        if not self.store.steps(job.id):
            for name, _ in plan:
                self.store.add_step(job.id, name)

        if self.isolate:
            try:
                workdir = worktree.create(Path(job.repo), job.id)
            except worktree.GitError as exc:
                self.store.set_status(job.id, "failed")
                self._log(f"Job {job.id}: cannot start: {exc}")
                return "failed"
        else:
            workdir = Path(job.repo)

        if self.store.reset_interrupted_steps(job.id):
            self._log(f"Job {job.id}: resuming an interrupted step; discarding its unfinished edits.")
            if self.isolate:
                worktree.discard_uncommitted(workdir)
        steps = self.store.steps(job.id)
        step = next((s for s in steps if s["status"] == "pending"), None)
        if step is None:
            self._finish(job, "done")
            return "finished"

        fn = dict(plan).get(step["name"])
        self.store.set_status(job.id, "running")
        if fn is None:
            self.store.set_step(step["id"], "failed", f"unknown step {step['name']!r}")
            self._finish(job, "failed")
            return "failed"
        self.store.set_step(step["id"], "running")
        ctx = StepContext(job, step["id"], step["name"], self.store, self.client, decision, workdir)
        try:
            output = fn(ctx)
        except BusyError as exc:
            self.store.reset_interrupted_steps(job.id)
            self.gate.force_busy(f"BharatCode answered busy ({exc})")
            self._log(f"Job {job.id}: BharatCode is busy; pausing.")
            return "busy"
        except AuthError:
            self.store.reset_interrupted_steps(job.id)
            raise
        except (BharatCodeError, worktree.GitError) as exc:
            self.store.set_step(step["id"], "failed", str(exc))
            self._finish(job, "failed")
            self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) failed: {exc}")
            return "failed"

        if self.isolate:
            # Commit before marking the step done, so "done" always means "saved on the branch".
            commit_id = worktree.commit(
                workdir, f"gtu {job.kind} {job.target}: {step['name']} (job {job.id})", exclude=NEVER_COMMIT
            )
            if commit_id:
                output = f"{output}\n\n[committed {commit_id[:10]} on {worktree.branch_name(job.id)}]"
        self.store.set_step(step["id"], "done", output)
        self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) done.")
        if self.store.get_job(job.id).status == "cancelled":
            self._finish(job, "cancelled")
        elif all(s["status"] == "done" for s in self.store.steps(job.id)):
            self._finish(job, "done")
            self._log(f"Job {job.id}: finished; results are on branch {worktree.branch_name(job.id)}.")
        return "done"

    def _finish(self, job: Job, status: str) -> None:
        """Record the final status and remove the job's worktree. Its branch is kept."""
        if status != "cancelled":
            self.store.set_status(job.id, status)
        if self.isolate:
            try:
                worktree.remove(Path(job.repo), job.id)
            except worktree.GitError as exc:
                self._log(f"Job {job.id}: could not remove its worktree: {exc}")
