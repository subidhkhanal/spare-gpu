"""What work actually cost in tokens (`spare_token_cost`, `gtu cost`), from the request log.

Every request we send is logged, and so is every step the BharatCode CLI reports. Tokens are split
into overhead (what's sent before the actual request: system prompt, tool schemas, project context)
and task (our prompt, the code it works on, and the model's output), and the overhead multiplier
is total / task.

What we can and can't separate:
- BharatCode CLI steps report one input number. Its fixed part, the system prompt and tool
  schemas, was measured on an empty folder (research/token-overhead.md); that much of each
  step's input counts as overhead, the rest as task. The conversation each step re-sends counts
  as task, so the multiplier is a lower bound.
- Direct requests (reviews, checks) carry only spare-gpu's own few lines of instructions, so they
  count as task: multiplier about 1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import Store, StoreError

CLI_ENDPOINT = "bharatcode-cli"
DEFAULT_HOURS = 24
# Measured 31 Oct 2026, BharatCode CLI 1.15.46, empty folder, "Reply with the single word OK.":
# 11,504 input tokens, of which ~2,025 system prompt and ~9,480 built-in tool schemas.
CLI_FLOOR_TOKENS = 11_500
FLOOR_ENV = "GTU_CLI_OVERHEAD_TOKENS"


def cli_floor() -> int:
    raw = os.environ.get(FLOOR_ENV, "").strip()
    return int(raw) if raw.isdigit() else CLI_FLOOR_TOKENS


@dataclass
class Tally:
    requests: int = 0
    prompt: int = 0
    completion: int = 0
    reasoning: int = 0
    total: int = 0
    overhead: int = 0

    @property
    def task(self) -> int:
        return self.total - self.overhead

    @property
    def multiplier(self) -> float | None:
        """Total tokens per task token; None when there's no task to divide by."""
        return self.total / self.task if self.task > 0 else None

    def add(self, row, floor: int = 0) -> None:
        self.requests += 1
        self.prompt += row["prompt_tokens"]
        self.completion += row["completion_tokens"]
        self.reasoning += row["reasoning_tokens"]
        self.total += row["total_tokens"]
        self.overhead += min(floor, row["prompt_tokens"])


@dataclass
class Cost:
    api: Tally = field(default_factory=Tally)
    cli: Tally = field(default_factory=Tally)
    jobs: dict[str, Tally] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.api.total + self.cli.total

    @property
    def overhead(self) -> int:
        return self.api.overhead + self.cli.overhead

    @property
    def task(self) -> int:
        return self.total - self.overhead

    @property
    def multiplier(self) -> float | None:
        return self.total / self.task if self.task > 0 else None


def collect(store: Store, scope: str | None, now: datetime | None = None) -> tuple[Cost, str]:
    """Tokens for a scope: a job id, "all", or a number of hours (default: the last 24).
    Returns the tally and a description of what it covers."""
    now = now or datetime.now(timezone.utc)
    scope = (scope or "").strip().lower()
    if scope in ("", "last_night", "last night", "today", "session", "tonight"):
        scope = str(DEFAULT_HOURS)
    if scope == "all":
        rows, label = store.conn.execute("SELECT * FROM requests").fetchall(), "all time"
    elif scope.replace(".", "", 1).isdigit():
        since = (now - timedelta(hours=float(scope))).isoformat()
        rows = store.conn.execute("SELECT * FROM requests WHERE sent_at >= ?", (since,)).fetchall()
        label = f"the last {float(scope):g} hours"
    else:
        job = store.get_job(scope)  # StoreError for an unknown id
        rows = store.requests(job.id)
        label = f"job {job.id} ({job.kind} {job.target})"
    cost, floor = Cost(), cli_floor()
    for row in rows:
        is_cli = row["endpoint"] == CLI_ENDPOINT
        (cost.cli if is_cli else cost.api).add(row, floor if is_cli else 0)
        if row["job_id"]:
            cost.jobs.setdefault(row["job_id"], Tally()).add(row, floor if is_cli else 0)
    return cost, label


def _times(value: float | None) -> str:
    return f"{value:.1f}x" if value is not None else "n/a"


def describe(store: Store, scope: str | None, now: datetime | None = None) -> str:
    try:
        cost, label = collect(store, scope, now)
    except StoreError as exc:
        return f"{exc}. Give a job id, \"all\", or a number of hours."
    if not cost.total:
        return f"No tokens used in {label}."
    floor = cli_floor()
    lines = [f"Tokens used in {label}: {cost.total:,}. Overhead {cost.overhead:,}, task {cost.task:,}: "
             f"overhead multiplier {_times(cost.multiplier)} (total / task)."]
    if cost.cli.requests:
        lines.append(
            f"- BharatCode CLI (tests and docs jobs): {cost.cli.total:,} over {cost.cli.requests} step(s). "
            f"Overhead {cost.cli.overhead:,}: its fixed ~{floor:,} tokens per step of system prompt and tool "
            f"schemas, re-sent every step. Task {cost.cli.task:,}: the rest of the input (your code, the "
            f"conversation so far) and {cost.cli.completion:,} output. Multiplier {_times(cost.cli.multiplier)}."
        )
    if cost.api.requests:
        lines.append(
            f"- Direct requests (reviews, checks): {cost.api.total:,} over {cost.api.requests} request(s), "
            f"{cost.api.prompt:,} input and {cost.api.completion:,} output"
            + (f" ({cost.api.reasoning:,} reasoning)" if cost.api.reasoning else "")
            + "; no harness overhead, so about 1.0x."
        )
    if len(cost.jobs) > 1 or (cost.jobs and scope and scope not in cost.jobs):
        top = sorted(cost.jobs.items(), key=lambda kv: -kv[1].total)[:5]
        lines.append("- By job: " + "; ".join(f"{job_id} {t.total:,} ({_times(t.multiplier)})" for job_id, t in top) + ".")
    if cost.cli.requests:
        lines.append(f"Can't separate exactly: the CLI reports one input number per step, so the ~{floor:,}-token "
                     "floor is our measurement (empty folder) rather than per-request data, and re-sent "
                     "conversation counts as task. The real multiplier is at least this.")
    return "\n".join(lines)
