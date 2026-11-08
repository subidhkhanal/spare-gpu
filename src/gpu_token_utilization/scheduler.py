"""Runs a job one step at a time, in front of the user, as BharatCode Desktop sessions.

Each job works on its own private copy of the project (worktree.py); the user's files
are never modified. Progress is saved after every step: the step is saved in the copy's
private history and marked done. A finished job's edited files are copied to
spare-gpu/<date>_<time>/ and its copy is deleted. A step left 'running' by a killed process never
finished, so on restart its partial edits are discarded and it runs again; finished
steps never do.

Work starts at once. When a Desktop session doesn't finish in time, the step waits a little and is
tried again, waiting longer each time it happens in a row. The token budget per job stops a job
when reached; it never fails it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import landing, ledger, router, waits, worktree
from .agent import AgentStep, BharatCodeError, BusyError, ChatResult, Usage
from .desktop import DESKTOP_NEEDED, DesktopAgent, DesktopNotFound
from .router import Choice
from .budget import BudgetExceeded, Budgets
from .jobs import PLANS, JobConfig
from .runstate import StopRequested
from .store import FINAL_STATUSES, Job, RequestLog, Store

# Files jobs may create for their own tooling that must never be committed.
NEVER_COMMIT = ("opencode.json",)
AGENT_ENDPOINT = "bharatcode-desktop"  # how an agent step is logged in the store


@dataclass
class StepContext:
    job: Job
    step_id: int
    step_name: str
    store: Store
    workdir: Path  # the job's worktree (or the repo itself when isolation is off, in tests)
    state_dir: Path  # per-job scratch space outside the worktree (.gtu/jobs/<id>), never committed
    config: JobConfig
    agent: DesktopAgent | None
    budget_remaining: Callable[[], int]
    should_stop: Callable[[], bool] = lambda: False
    log: Callable[[str], None] = lambda message: None

    def chat(self, messages: list[dict]) -> ChatResult:
        """One answer, as a read-only Desktop session (the review job's step)."""
        if self.agent is None:
            raise DesktopNotFound(DESKTOP_NEEDED)
        self.log("  Asking BharatCode Desktop...")
        started = time.monotonic()
        result = self.agent.ask(self.workdir, messages, token_budget=self.budget_remaining(),
                                on_step=self.log_agent_step, should_stop=self.should_stop)
        if result.stopped == "stop":
            raise StopRequested("stopped")
        if result.stopped == "budget":
            raise BudgetExceeded(f"stopped BharatCode Desktop after {result.tokens} tokens: budget reached")
        if result.stopped == "timeout":
            raise BusyError(f"BharatCode Desktop did not finish within {self.agent.timeout:.0f} s")
        if not result.ok:
            raise BharatCodeError(f"BharatCode Desktop failed: {result.error or 'unknown error'}")
        inputs = sum(s.input_tokens + s.cache_read_tokens for s in result.steps)
        outputs = sum(s.output_tokens + s.reasoning_tokens for s in result.steps)
        return ChatResult(content=result.text,
                          usage=Usage(prompt_tokens=inputs, completion_tokens=outputs, total_tokens=result.tokens),
                          elapsed_ms=(time.monotonic() - started) * 1000)

    def log_agent_step(self, step: AgentStep) -> None:
        """Record one step of a Desktop session like a request, so budgets and reports include it."""
        took = f" in {step.elapsed_ms / 1000:.0f} s" if step.elapsed_ms else ""
        self.log(f"  {_label(self.agent)} finished a step{took} ({step.total_tokens:,} tokens).")
        self.store.log_request(
            RequestLog(
                endpoint=AGENT_ENDPOINT, model=self.agent.model if self.agent else None, attempt=1, status_code=None,
                elapsed_ms=step.elapsed_ms, prompt_tokens=step.input_tokens + step.cache_read_tokens,
                completion_tokens=step.output_tokens, reasoning_tokens=step.reasoning_tokens,
                total_tokens=step.total_tokens, job_id=self.job.id, step_id=self.step_id,
            )
        )


class _ToolRecorder:
    """The job's worker, with the tools each run reports kept for the job's ledger. Everything
    else (model, label, timeout, with_model) is the worker's own."""

    def __init__(self, worker):
        self._worker = worker
        self.tools: list[str] = []

    def __getattr__(self, name):
        return getattr(self._worker, name)

    def run(self, *args, **kwargs):
        result = self._worker.run(*args, **kwargs)
        self.tools += result.tools
        return result

    def ask(self, *args, **kwargs):
        result = self._worker.ask(*args, **kwargs)
        self.tools += result.tools
        return result


def _label(worker) -> str:
    return getattr(worker, "label", "BharatCode Desktop")


def _model_name(worker) -> str:
    return waits.name(getattr(worker, "model", "") or "")


KEPT_NOTHING = re.compile(r"^(No test files were generated|Kept 0 passing test|No documentation changes were "
                          r"generated|Kept documentation changes in 0 file|No fix was generated|Kept the fix in 0 file|"
                          r"Tests still fail after|Nothing was implemented|Tests fail after implementing the plan)")


def kept_nothing(verify_output: str | None) -> bool:
    """Whether a verify step's summary says nothing passed the check (the router's fallback trigger)."""
    return bool(KEPT_NOTHING.match(verify_output or ""))


StepFn = Callable[[StepContext], str]
Plan = list[tuple[str, StepFn]]

DEFAULT_PLANS: dict[str, Plan] = PLANS

BUSY_WAIT = 60.0  # seconds before trying a step again after a session didn't finish in time...
BUSY_WAIT_MAX = 600.0  # ...doubling each time it happens in a row, up to this


class Scheduler:
    def __init__(
        self,
        store: Store,
        *,
        plans: dict[str, Plan] | None = None,
        agent: DesktopAgent | None = None,
        budgets: Budgets | None = None,
        config: JobConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        log: Callable[[str], None] = lambda message: None,
        isolate: bool = True,
        should_stop: Callable[[], bool] | None = None,
        route: Callable[[Job], Choice] | None = None,
    ):
        self.store = store
        # Picks the model for a job before its first step (router.choose with fresh wait figures);
        # None keeps every job on the worker's own model.
        self.route = route
        self.plans = plans or DEFAULT_PLANS
        self.agent = agent
        self.budgets = budgets or Budgets()
        self.config = config or JobConfig()
        self._sleep = sleep
        self._now = now
        self._log = log
        self.isolate = isolate
        self.busy_wait = BUSY_WAIT  # grows while sessions keep not finishing in time, resets on a finished step
        # A stop (the job cancelled from the chat): checked between steps, about once a second while
        # waiting, and while a session runs.
        self._interruptible = should_stop is not None
        self.should_stop = should_stop or (lambda: False)
        self.stopped = False

    def _wait(self, seconds: float) -> None:
        """Sleep, waking about once a second to notice a stop."""
        if not self._interruptible:
            self._sleep(seconds)
            return
        left = seconds
        while left > 0 and not self.should_stop():
            chunk = min(1.0, left)
            self._sleep(chunk)
            left -= chunk

    def _over_budget(self, job: Job) -> str | None:
        """Why this job can't run now because of its budget, or None."""
        job_used = self.budgets.used(self.store, job.id)
        if job_used >= self.budgets.per_job:
            return f"its token budget is used up ({job_used} of {self.budgets.per_job})"
        return None

    def run_job(self, job_id: str) -> str:
        """Run one job to its end, in front of whoever asked. Returns its final status: done, failed
        or cancelled; paused when its budget ran out or a stop was asked for."""
        while True:
            job = self.store.get_job(job_id)
            if job.status in FINAL_STATUSES:
                return job.status
            if self.should_stop():
                self._stop_now()
                return self.store.get_job(job_id).status
            reason = self._over_budget(job)
            if reason is not None:
                self.store.set_status(job.id, "paused")
                self._log(f"Job {job.id}: stopped, {reason}.")
                return "paused"
            outcome = self.run_step(job)
            if outcome == "stopped":
                if self.store.get_job(job_id).status == "cancelled":  # spare_cancel_work
                    self._finish(job, "cancelled")
                    self._log(f"Job {job.id}: cancelled; its copy is removed and nothing was exported.")
                    return "cancelled"
                self._stop_now()
                return "paused"
            if outcome == "busy":
                self._wait_out_busy()
            elif outcome == "budget":
                return "paused"
            elif outcome == "done":
                self.busy_wait = BUSY_WAIT
            elif outcome in ("failed", "finished"):
                return self.store.get_job(job_id).status

    def _wait_out_busy(self) -> None:
        """A session didn't finish in time: wait, longer each time in a row, then try the step again."""
        self._log(f"BharatCode is busy; trying again in {self.busy_wait:.0f} s.")
        self._wait(self.busy_wait)
        self.busy_wait = min(self.busy_wait * 2, BUSY_WAIT_MAX)

    def run_step(self, job: Job) -> str:
        """Run the job's next unfinished step. Returns done, busy, budget, failed, retry, stopped or finished."""
        plan = self.plans[job.kind]
        if not self.store.steps(job.id):
            for name, _ in plan:
                self.store.add_step(job.id, name)

        if self.isolate:
            try:
                workdir = worktree.create(Path(job.repo), job.id,
                                          warn=lambda note: self._log(f"Job {job.id}: {note}"))
                if not job.base_commit:  # first run: remember the copy's starting point, for reports
                    self.store.set_base_commit(job.id, worktree._git(workdir, "rev-parse", "HEAD").stdout.strip())
            except (worktree.GitError, OSError) as exc:
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
            self._complete(job)
            return "finished"

        fn = dict(plan).get(step["name"])
        job, worker = self._route(job)
        self.store.set_status(job.id, "running")
        if fn is None:
            self.store.set_step(step["id"], "failed", f"unknown step {step['name']!r}")
            self._finish(job, "failed")
            return "failed"
        self.store.set_step(step["id"], "running")
        recorder = _ToolRecorder(worker) if worker is not None else None
        ctx = StepContext(
            job, step["id"], step["name"], self.store, workdir, state_dir,
            self.config, recorder, lambda: self.budgets.remaining(self.store, job.id),
            self.should_stop, self._log,
        )
        doing = f"{_label(worker)} is working on it ({_model_name(worker)}); this can take several minutes" \
            if step["name"] == "write" else "running locally" if step["name"] == "verify" else "one request"
        self._log(f"Job {job.id}: step {step['seq']} of {len(steps)} ({step['name']}) started: {doing}.")
        # For the ledger: when the step began, and which requests were logged before it (so a step
        # run again after a fallback counts only its own).
        started, requests_before = self._now(), max((r["id"] for r in self.store.requests(job.id)), default=0)
        try:
            output = fn(ctx)
        except StopRequested:
            self._undo_step(job, workdir)
            self.store.set_status(job.id, "paused")
            return "stopped"
        except BusyError as exc:
            self._undo_step(job, workdir)
            self._log(f"Job {job.id}: BharatCode answered busy ({exc}).")
            return "busy"
        except BudgetExceeded as exc:
            self._undo_step(job, workdir)
            self.store.set_status(job.id, "paused")
            self._log(f"Job {job.id}: stopped, {exc}.")
            return "budget"
        except (BharatCodeError, worktree.GitError, DesktopNotFound) as exc:
            if self.should_stop():  # failed because we were being stopped: that's a pause, not a failure
                self._undo_step(job, workdir)
                self.store.set_status(job.id, "paused")
                return "stopped"
            allowed = router.plan_refusal(str(exc))
            if allowed and worker is not None and waits.model_id(worker.model) != allowed:
                # The user's plan doesn't include this model: remember that, and rerun the step on
                # the one it does include instead of failing the job.
                self._undo_step(job, workdir)
                folder = Path(job.repo) / ".gtu"
                state = router.State.load(folder)
                state.mark_unavailable(worker.model)
                state.save(folder)
                self.store.set_route(job.id, allowed, allowed,
                                     f"your BharatCode plan doesn't include {waits.name(worker.model)}")
                self._log(f"Job {job.id}: your BharatCode plan doesn't include {waits.name(worker.model)}; "
                          f"running it on {waits.name(allowed)} instead.")
                return "retry"
            self.store.set_step(step["id"], "failed", str(exc))
            self._record(job, step, state_dir, workdir, recorder, started, requests_before, "failed", str(exc))
            self._finish(job, "failed")
            self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) failed: {exc}")
            return "failed"
        except Exception as exc:  # noqa: BLE001  anything else (a git call, a truncated junit.xml, a locked file)
            # fails this job and is written down, instead of the server dying with a traceback and
            # leaving the job marked running.
            if self.should_stop():
                self._undo_step(job, workdir)
                self.store.set_status(job.id, "paused")
                return "stopped"
            what = f"{type(exc).__name__}: {exc}"
            self.store.set_step(step["id"], "failed", what)
            self._record(job, step, state_dir, workdir, recorder, started, requests_before, "failed", what)
            self._finish(job, "failed")
            self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) failed unexpectedly ({what}); the other jobs carry on.")
            return "failed"

        if self.isolate:
            # Save before marking the step done, so "done" always means "kept in the job's copy".
            worktree.commit(workdir, f"gtu {job.kind} {job.target}: {step['name']} (job {job.id})",
                            exclude=NEVER_COMMIT)
        self.store.set_step(step["id"], "done", output)
        self._record(job, step, state_dir, workdir, recorder, started, requests_before, "done", output)
        self._log(f"Job {job.id}: step {step['seq']} ({step['name']}) done.")
        if step["name"] == "verify" and kept_nothing(output) and self._fall_back(job):
            return "done"
        if self.store.get_job(job.id).status == "cancelled":
            self._finish(job, "cancelled")
        elif all(s["status"] == "done" for s in self.store.steps(job.id)):
            self._complete(job)
        return "done"

    def _route(self, job: Job) -> tuple[Job, DesktopAgent | None]:
        """The job and the worker for it, on the job's model. The model is chosen once, before the
        first step, and stored: every later step and retry of the job uses the same one."""
        if job.model is None and self.route is not None and self.agent is not None:
            choice = self.route(job)
            self.store.set_route(job.id, choice.model, choice.preferred, choice.reason)
            self._log(f"Job {job.id}: {choice.summary()}")
            job = self.store.get_job(job.id)
        worker = self.agent
        if worker is not None and job.model and waits.model_id(worker.model) != waits.model_id(job.model):
            worker = worker.with_model(f"bharatcode/{waits.model_id(job.model)}")
        return job, worker

    def _fall_back(self, job: Job) -> bool:
        """A routed job whose check kept nothing runs once more, on the preferred model."""
        job = self.store.get_job(job.id)
        if not job.model or not job.preferred_model or job.fallback or \
                waits.model_id(job.model) == waits.model_id(job.preferred_model):
            return False
        self.store.set_route(job.id, job.preferred_model, job.preferred_model,
                             f"fell back from {waits.name(job.model)}: its result kept nothing", fallback=True)
        self.store.reset_steps(job.id)
        self._log(f"Job {job.id}: nothing from {waits.name(job.model)} passed the check; running it once more on "
                  f"{waits.name(job.preferred_model)}.")
        return True

    def _stop_now(self) -> None:
        """A stop was asked for: leave every started job paused."""
        self.stopped = True
        for job in self.store.list_jobs(include_finished=False):
            if job.status == "running":
                self.store.set_status(job.id, "paused")
        self._log("Stopped.")

    def _undo_step(self, job: Job, workdir: Path) -> None:
        """Put an unfinished step back to pending and drop its edits, so it reruns cleanly."""
        self.store.reset_interrupted_steps(job.id)
        if self.isolate:
            worktree.discard_uncommitted(workdir)

    def _record(self, job: Job, step, state_dir: Path, workdir: Path, worker, started: datetime,
                requests_before: int, status: str, outcome: str | None) -> None:
        """One ledger entry for a step that finished or failed, written now: a crash later still
        leaves the record. Tokens and the model come from the requests the step logged; the tools
        from what the worker reported."""
        rows = [r for r in self.store.requests(job.id) if r["id"] > requests_before]
        model = next((r["model"] for r in reversed(rows) if r["model"]), None) \
            or getattr(worker, "model", None) or self.store.get_job(job.id).model
        try:
            ledger.record(
                state_dir, job, step=step["name"], status=status, model=waits.model_id(model) if model else None,
                started=started, finished=self._now(), tokens=sum(r["total_tokens"] or 0 for r in rows),
                tools=ledger.relative_tools(getattr(worker, "tools", []), workdir if self.isolate else None),
                outcome=outcome,
            )
        except OSError as exc:
            self._log(f"Job {job.id}: couldn't write its ledger ({exc}); the job carries on.")

    def _complete(self, job: Job) -> None:
        """A finished job: its edited files go to spare-gpu/<date>_<time>/, then its copy is deleted.
        If copying fails, the copy is kept so nothing is lost."""
        try:
            folder = self._export(job)
        except (worktree.GitError, OSError) as exc:
            self.store.set_status(job.id, "done")
            self._log(f"Job {job.id}: finished, but couldn't copy its files to {worktree.OUTPUT_DIR}/: {exc}. "
                      f"They're kept in {worktree.worktree_path(Path(job.repo), job.id)}.")
            return
        self._finish(job, "done")
        self._log(f"Job {job.id}: finished; " + (f"its edited files are in {folder}." if folder
                                                 else "it changed no files."))

    def _export(self, job: Job) -> str | None:
        """Copy the finished job's edited files to <repo>/spare-gpu/<date>_<time>/, write RUN.md and
        manifest.json next to them (from the job's ledger and steps, and the base versions still in
        its copy), and record the folder. The path is given relative to the repo, as the user sees
        it there."""
        if not self.isolate:
            return None
        repo = Path(job.repo)
        when = self._now()
        folder = worktree.export(repo, job.id, job.base_commit, when.astimezone())
        if folder is None:
            return None
        job = self.store.get_job(job.id)  # with the model the router gave it
        landing.write_records(
            folder, worktree.worktree_path(repo, job.id), job, base=job.base_commit,
            ledger_doc=ledger.read(repo / ".gtu" / "jobs" / job.id), steps=[dict(s) for s in self.store.steps(job.id)],
            tokens=self.store.request_summary(job.id)["total_tokens"], exported_at=when,
        )
        shown = folder.relative_to(repo).as_posix()
        self.store.set_output_dir(job.id, shown)
        return shown

    def _finish(self, job: Job, status: str) -> None:
        """Record the final status and delete the job's copy of the project."""
        if status != "cancelled":
            self.store.set_status(job.id, status)
        if self.isolate:
            try:
                worktree.remove(Path(job.repo), job.id)
            except worktree.GitError as exc:
                self._log(f"Job {job.id}: could not remove its worktree: {exc}")
