"""Runs queued jobs one step at a time, only while the capacity gate is open.

Each job works in its own git worktree on branch gtu/<job-id>; the user's checkout
is never modified. Progress is saved after every step: the step is committed on the
job's branch and marked done. A step left 'running' by a killed process never
finished, so on restart its partial edits are discarded and it runs again; finished
steps never do.

Token budgets (per job and per night) pause work when reached; they never fail a job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import worktree
from .agent_cli import CLI_KINDS, BharatCodeCli, CliNotFound, CliStep
from .budget import BudgetExceeded, Budgets
from .client import AuthError, BharatCodeClient, BharatCodeError, BusyError, ChatResult
from .gate import Decision, Gate
from .jobs import PLANS, JobConfig
from .runstate import StopRequested
from .store import Job, RequestLog, Store

# Files jobs may create for their own tooling that must never be committed.
NEVER_COMMIT = ("opencode.json",)
CLI_ENDPOINT = "bharatcode-cli"


@dataclass
class StepContext:
    job: Job
    step_id: int
    step_name: str
    store: Store
    client: BharatCodeClient
    decision: Decision
    workdir: Path  # the job's worktree (or the repo itself when isolation is off, in tests)
    state_dir: Path  # per-job scratch space outside the worktree (.gtu/jobs/<id>), never committed
    config: JobConfig
    cli: BharatCodeCli | None
    budget_remaining: Callable[[], int]
    should_stop: Callable[[], bool] = lambda: False
    log: Callable[[str], None] = lambda message: None

    def chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.log("  Sending request to BharatCode...")
        result = self.client.chat(
            messages, job_id=self.job.id, step_id=self.step_id, gate=self.decision.gate_info, **kwargs
        )
        waited = f", {result.queue_wait_ms / 1000:.1f} s of it in BharatCode's queue" if result.queue_wait_ms else ""
        self.log(f"  Answer received in {result.elapsed_ms / 1000:.1f} s ({result.usage.total_tokens:,} tokens{waited}).")
        return result

    def log_cli_step(self, step: CliStep) -> None:
        """Record one BharatCode CLI step like a request, so budgets and reports include it."""
        took = f" in {step.elapsed_ms / 1000:.0f} s" if step.elapsed_ms else ""
        self.log(f"  BharatCode CLI finished a step{took} ({step.total_tokens:,} tokens).")
        self.store.log_request(
            RequestLog(
                endpoint=CLI_ENDPOINT, model=self.cli.model if self.cli else None, attempt=1, status_code=None,
                elapsed_ms=step.elapsed_ms, prompt_tokens=step.input_tokens + step.cache_read_tokens,
                completion_tokens=step.output_tokens, reasoning_tokens=step.reasoning_tokens,
                total_tokens=step.total_tokens, job_id=self.job.id, step_id=self.step_id,
                **self.decision.gate_info,
            )
        )


StepFn = Callable[[StepContext], str]
Plan = list[tuple[str, StepFn]]

DEFAULT_PLANS: dict[str, Plan] = PLANS

_STATUS_ORDER = {"running": 0, "paused": 1, "queued": 2}


class Scheduler:
    def __init__(
        self,
        store: Store,
        gate: Gate,
        client: BharatCodeClient,
        *,
        plans: dict[str, Plan] | None = None,
        cli: BharatCodeCli | None = None,
        budgets: Budgets | None = None,
        config: JobConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        log: Callable[[str], None] = lambda message: None,
        isolate: bool = True,
        ignore_gate: bool = False,
        should_stop: Callable[[], bool] | None = None,
        cli_missing: str | None = None,
    ):
        self.store = store
        # Why the BharatCode CLI can't be used, if it can't: tests and docs jobs then stay
        # queued (not failed) and the run carries on with review jobs.
        self.cli_missing = cli_missing
        self.gate = gate
        self.client = client
        self.plans = plans or DEFAULT_PLANS
        self.cli = cli
        self.budgets = budgets or Budgets()
        self.config = config or JobConfig()
        self._sleep = sleep
        self._now = now
        self._log = log
        self.isolate = isolate
        self.ignore_gate = ignore_gate  # the default; False only with --wait-for-capacity
        # `gtu stop`: checked between steps, about once a second while waiting, and by the CLI.
        self._interruptible = should_stop is not None
        self.should_stop = should_stop or (lambda: False)
        self.stopped = False

    def _wait(self, seconds: float) -> None:
        """Sleep, waking about once a second to notice `gtu stop`."""
        if not self._interruptible:
            self._sleep(seconds)
            return
        left = seconds
        while left > 0 and not self.should_stop():
            chunk = min(1.0, left)
            self._sleep(chunk)
            left -= chunk

    def pending_jobs(self) -> list[Job]:
        jobs = self.store.list_jobs(include_finished=False)
        return sorted(jobs, key=lambda j: _STATUS_ORDER.get(j.status, 3))

    def _over_budget(self, job: Job) -> str | None:
        """Why this job can't run now because of a budget, or None."""
        job_used, night_used = self.budgets.used(self.store, job.id, self._now())
        if night_used >= self.budgets.per_night:
            return f"tonight's token budget is used up ({night_used} of {self.budgets.per_night})"
        if job_used >= self.budgets.per_job:
            return f"its token budget is used up ({job_used} of {self.budgets.per_job})"
        return None

    def run(self, stop: Callable[[], bool] = lambda: False, max_steps: int | None = None) -> int:
        """Work through the queue until it is empty or every job left is over budget."""
        completed = 0
        paused_for_budget: set[str] = set()
        waiting_for_cli: set[str] = set()
        queued = self.pending_jobs()
        if queued:
            self._log(f"{len(queued)} job(s) to run. "
                      + ("Starting now." if self.ignore_gate else "Waiting for a free slot first (--wait-for-capacity)."))
        gate_was_open = False
        while not stop():
            if self.should_stop():
                self._stop_now()
                return completed
            runnable = []
            for job in self.pending_jobs():
                if self.cli_missing and job.kind in CLI_KINDS:
                    if job.id not in waiting_for_cli:
                        waiting_for_cli.add(job.id)
                        self._log(f"Job {job.id} ({job.kind}): skipped, it needs BharatCode's CLI, which "
                                  "isn't available. It stays queued for the next `gtu start`.")
                    continue
                reason = self._over_budget(job)
                if reason is None:
                    runnable.append(job)
                elif job.id not in paused_for_budget:
                    paused_for_budget.add(job.id)
                    self.store.set_status(job.id, "paused")
                    self._log(f"Job {job.id}: paused, {reason}. Raise the budget to let it continue.")
            if not runnable:
                if waiting_for_cli:
                    self._log(f"{len(waiting_for_cli)} job(s) are waiting for BharatCode's CLI. {self.cli_missing}")
                elif paused_for_budget:
                    self._log("Nothing left that fits the token budgets.")
                else:
                    self._log("Queue empty.")
                return completed
            job = runnable[0]
            decision = self.gate.check_bypassed() if self.ignore_gate else self.gate.check()
            if not decision.open:
                gate_was_open = False
                if job.status == "running":
                    self.store.set_status(job.id, "paused")
                self._log(decision.describe())
                self._wait(decision.next_check)
                continue
            if not gate_was_open:
                self._log(decision.describe())
                gate_was_open = True
            outcome = self.run_step(job, decision)
            if outcome == "stopped":
                self._stop_now()
                return completed
            if outcome == "done":
                completed += 1
                if max_steps is not None and completed >= max_steps:
                    return completed
        return completed

    def run_step(self, job: Job, decision: Decision) -> str:
        """Run the job's next unfinished step. Returns done, busy, budget, failed or finished."""
        plan = self.plans[job.kind]
        if not self.store.steps(job.id):
            for name, _ in plan:
                self.store.add_step(job.id, name)

        if self.isolate:
            try:
                workdir = worktree.create(Path(job.repo), job.id)
                if not job.base_commit:  # first run: remember where the branch started, for reports
                    self.store.set_base_commit(job.id, worktree._git(workdir, "rev-parse", "HEAD").stdout.strip())
            except worktree.GitError as exc:
                self.store.set_status(job.id, "failed")
                self._log(f"Job {job.id}: cannot start: {exc}")
                return "failed"
        else:
            workdir = Path(job.repo)
        state_dir = Path(job.repo) / ".gtu" / "jobs" / job.id
        state_dir.mkdir(parents=True, exist_ok=True)

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
        ctx = StepContext(
            job, step["id"], step["name"], self.store, self.client, decision, workdir, state_dir,
            self.config, self.cli, lambda: self.budgets.remaining(self.store, job.id, self._now()),
            self.should_stop, self._log,
        )
        doing = "the BharatCode CLI is working on it; this can take several minutes" \
            if step["name"] == "write" else "running locally" if step["name"] == "verify" else "one request"
        self._log(f"Job {job.id}: step {step['seq']} of {len(steps)} ({step['name']}) started: {doing}.")
        main_before = self._main_state(job)
        try:
            output = fn(ctx)
        except StopRequested:
            self._undo_step(job, workdir)
            self.store.set_status(job.id, "paused")
            return "stopped"
        except BusyError as exc:
            self._undo_step(job, workdir)
            self.gate.force_busy(f"BharatCode answered busy ({exc})")
            self._log(f"Job {job.id}: BharatCode is busy; pausing.")
            return "busy"
        except BudgetExceeded as exc:
            self._undo_step(job, workdir)
            self.store.set_status(job.id, "paused")
            self._log(f"Job {job.id}: paused, {exc}.")
            return "budget"
        except AuthError:
            self._undo_step(job, workdir)
            raise
        except (BharatCodeError, worktree.GitError, CliNotFound) as exc:
            if self.should_stop():  # failed because we were being stopped: that's a pause, not a failure
                self._undo_step(job, workdir)
                self.store.set_status(job.id, "paused")
                return "stopped"
            self.store.set_step(step["id"], "failed", str(exc))
            self._finish(job, "failed")
            self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) failed: {exc}")
            return "failed"

        if main_before is not None and self._main_state(job) != main_before:
            message = "your main checkout changed while this step ran; stopping the job to be safe"
            self.store.set_step(step["id"], "failed", message)
            self._finish(job, "failed")
            self._log(f"Job {job.id}: {message}. Check `git status` in {job.repo}.")
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

    def _stop_now(self) -> None:
        """`gtu stop` was requested: leave every started job paused and resumable."""
        self.stopped = True
        for job in self.store.list_jobs(include_finished=False):
            if job.status == "running":
                self.store.set_status(job.id, "paused")
        self._log("Stopped by `gtu stop`. Progress is saved; run `gtu start` to resume.")

    def _main_state(self, job: Job) -> tuple[str, str] | None:
        """HEAD and status of the user's checkout, to prove a step left it alone."""
        if not self.isolate:
            return None
        repo = Path(job.repo)
        return worktree._git(repo, "rev-parse", "HEAD").stdout, worktree._git(repo, "status", "--porcelain").stdout

    def _undo_step(self, job: Job, workdir: Path) -> None:
        """Put an unfinished step back to pending and drop its edits, so it reruns cleanly."""
        self.store.reset_interrupted_steps(job.id)
        if self.isolate:
            worktree.discard_uncommitted(workdir)

    def _finish(self, job: Job, status: str) -> None:
        """Record the final status and remove the job's worktree. Its branch is kept."""
        if status != "cancelled":
            self.store.set_status(job.id, status)
        if self.isolate:
            try:
                worktree.remove(Path(job.repo), job.id)
            except worktree.GitError as exc:
                self._log(f"Job {job.id}: could not remove its worktree: {exc}")
