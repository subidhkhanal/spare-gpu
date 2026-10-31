"""`gtu start --dry-run`: what would run, and roughly what it would cost, without doing any of it.

Read-only: sends nothing (not even the live-stats poll), needs no API key, and creates no
branches, worktrees, steps or gate readings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import worktree
from .agent_cli import CliNotFound, resolve_command
from .api import ApiKeyMissing, find_api_key
from .budget import Budgets
from .jobs import PLANS, JobConfig, numbered_listing
from .store import Job, Store

CLI_STEP_TOKENS = 11_500  # measured: mostly the CLI's own system prompt, sent on every step
CLI_STEPS = {"tests": (3, 7), "docs": (3, 5)}  # read, edit, finish; tests may also run pytest
STEP_DOING = {
    ("tests", "write"): "BharatCode CLI writes new test files",
    ("tests", "verify"): "run them here, keep only passing tests, commit",
    ("docs", "write"): "BharatCode CLI adds docstrings and README text",
    ("docs", "verify"): "keep only additive doc changes, commit",
    ("review", "review"): "one request; report saved as gtu-review-<id>.md",
}


@dataclass
class StepPlan:
    name: str
    doing: str
    low: int
    high: int
    basis: str


@dataclass
class JobPlan:
    job: Job
    steps: list[StepPlan]
    used: int
    skip_reason: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def low(self) -> int:
        return sum(s.low for s in self.steps)

    @property
    def high(self) -> int:
        return sum(s.high for s in self.steps)


def _target_tokens(job: Job) -> tuple[int, int]:
    """(rough token count of the target code, characters of the review listing)."""
    listing, _ = numbered_listing(Path(job.repo), job.target, max_chars=40_000)
    return len(listing) // 4, len(listing)


def estimate(store: Store, job: Job, step: str, config: JobConfig) -> StepPlan:
    doing = STEP_DOING.get((job.kind, step), step)
    history = store.step_token_history(job.kind, step)
    if history:
        return StepPlan(step, doing, min(history), max(history), f"from {len(history)} earlier run(s)")
    if step == "verify":
        return StepPlan(step, doing, 0, 0, "runs locally, no requests")
    try:
        code_tokens, listing_chars = _target_tokens(job)
    except OSError:
        code_tokens, listing_chars = 0, 0
    if job.kind == "review":
        prompt = listing_chars // 3 + 500
        return StepPlan(step, doing, prompt + 300, prompt + config.review_max_tokens, "estimate from the code size")
    low_steps, high_steps = CLI_STEPS.get(job.kind, (3, 5))
    per_step = CLI_STEP_TOKENS + code_tokens
    return StepPlan(step, doing, low_steps * per_step, high_steps * per_step,
                    f"estimate: {low_steps}-{high_steps} CLI steps of ~{_rough(per_step)} tokens")


def plan_jobs(store: Store, budgets: Budgets, config: JobConfig, now: datetime) -> list[JobPlan]:
    order = {"running": 0, "paused": 1, "queued": 2}
    plans = []
    for job in sorted(store.list_jobs(include_finished=False), key=lambda j: order.get(j.status, 3)):
        existing = store.steps(job.id)
        names = [s["name"] for s in existing if s["status"] in ("pending", "running")] if existing \
            else [name for name, _ in PLANS[job.kind]]
        job_used, night_used = budgets.used(store, job.id, now)
        plan = JobPlan(job, [estimate(store, job, n, config) for n in names], job_used)
        if night_used >= budgets.per_night:
            plan.skip_reason = "tonight's token budget is used up"
        elif job_used >= budgets.per_job:
            plan.skip_reason = "its token budget is used up"
        plan.problems = _problems(job)
        plans.append(plan)
    return plans


def _problems(job: Job) -> list[str]:
    """Things that would stop this job when it starts. Read-only checks."""
    problems = []
    if not worktree.worktree_path(Path(job.repo), job.id).exists():  # resuming jobs already have one
        try:
            worktree.ensure_clean(Path(job.repo))
        except worktree.GitError as exc:
            problems.append(str(exc).splitlines()[0])
    if job.kind in ("tests", "docs"):
        try:
            resolve_command()
        except CliNotFound as exc:
            problems.append(str(exc))
    return problems


def _rough(n: int) -> str:
    """Estimates aren't precise; don't print them as if they were."""
    return f"{round(n, -3):,}" if n >= 10_000 else f"{round(n, -2):,}" if n >= 1_000 else f"{n:,}"


def describe(store: Store, budgets: Budgets, config: JobConfig, now: datetime, ignore_gate: bool) -> str:
    plans = plan_jobs(store, budgets, config, now)
    out = ["Dry run: nothing is sent, and no branches, worktrees or job steps are created.", ""]
    if not plans:
        return "\n".join(out + ["No queued jobs. Add one with `gtu add review <file>`."])
    low = high = 0
    for p in plans:
        j = p.job
        out.append(f"Job {j.id}  {j.kind}  {j.target}  ({j.status})")
        if p.skip_reason:
            out.append(f"  skipped: {p.skip_reason} ({p.used:,} used of {budgets.per_job:,} for this job)")
        for i, s in enumerate(p.steps, 1):
            cost = "0 tokens" if s.high == 0 else f"~{_rough(s.low)}-{_rough(s.high)} tokens"
            out.append(f"  {i}. {s.name:<7} {s.doing}")
            out.append(f"     {cost} ({s.basis})")
        if not p.skip_reason:
            remaining = budgets.per_job - p.used
            out.append(f"  job budget: {p.used:,} used, {remaining:,} left of {budgets.per_job:,}")
            if p.high > remaining:
                out.append("  note: the high estimate is over what's left, so this job may pause partway")
            low, high = low + p.low, high + p.high
        for problem in p.problems:
            out.append("  PROBLEM: " + problem.replace("\n", "\n    "))  # keep multi-line help under its job
        out.append("")
    night = store.tokens_since(budgets.night_began(now).isoformat())
    out.append(f"Total for jobs that would run: ~{_rough(low)}-{_rough(high)} tokens. "
               f"Tonight so far: {night:,} of {budgets.per_night:,}.")
    if ignore_gate:
        out.append("Start: immediately; BharatCode's busy answers are retried politely. "
                   "Add --wait-for-capacity to hold jobs for a free slot instead.")
    else:
        last = store.last_gate_reading()
        seen = f" Last reading ({last.taken_at[11:16]} UTC): {last.reason}." if last else ""
        out.append(f"Start: only once BharatCode's live stats show a free slot (--wait-for-capacity).{seen}")
    try:
        key_found = find_api_key() is not None
    except ApiKeyMissing:
        key_found = False
    if not key_found:
        out.append("PROBLEM: no BharatCode API key found (run `gtu setkey`); a real run would stop at once.")
    out.append("Estimates are rough: BharatCode CLI steps vary with how much the model reads and retries.")
    return "\n".join(out)
