"""What work actually cost in tokens (`spare_token_cost`, `gtu cost`), from the request log.

Every request we send is logged, and so is every step the BharatCode CLI reports, so tests and
docs jobs can be split into the CLI's own overhead (the input it re-sends on every step, mostly
its system prompt) and the work itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import Store, StoreError

CLI_ENDPOINT = "bharatcode-cli"
DEFAULT_HOURS = 24


@dataclass
class Tally:
    requests: int = 0
    prompt: int = 0
    completion: int = 0
    reasoning: int = 0
    total: int = 0

    def add(self, row) -> None:
        self.requests += 1
        self.prompt += row["prompt_tokens"]
        self.completion += row["completion_tokens"]
        self.reasoning += row["reasoning_tokens"]
        self.total += row["total_tokens"]


@dataclass
class Cost:
    api: Tally = field(default_factory=Tally)
    cli: Tally = field(default_factory=Tally)
    jobs: dict[str, int] = field(default_factory=dict)  # job id -> tokens

    @property
    def total(self) -> int:
        return self.api.total + self.cli.total


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
    cost = Cost()
    for row in rows:
        (cost.cli if row["endpoint"] == CLI_ENDPOINT else cost.api).add(row)
        if row["job_id"]:
            cost.jobs[row["job_id"]] = cost.jobs.get(row["job_id"], 0) + row["total_tokens"]
    return cost, label


def describe(store: Store, scope: str | None, now: datetime | None = None) -> str:
    try:
        cost, label = collect(store, scope, now)
    except StoreError as exc:
        return f"{exc}. Give a job id, \"all\", or a number of hours."
    if not cost.total:
        return f"No tokens used in {label}."
    lines = [f"Tokens used in {label}: {cost.total:,}."]
    if cost.cli.requests:
        per_step = cost.cli.prompt // cost.cli.requests
        lines.append(
            f"- BharatCode CLI (tests and docs jobs): {cost.cli.total:,} over {cost.cli.requests} step(s). "
            f"{cost.cli.prompt:,} of that is input the CLI sends on every step, about {per_step:,} per step, "
            f"mostly its own system prompt and the conversation so far; {cost.cli.completion:,} is output."
        )
    if cost.api.requests:
        lines.append(
            f"- Direct requests (reviews, checks): {cost.api.total:,} over {cost.api.requests} request(s): "
            f"{cost.api.prompt:,} input (your code and our instructions), {cost.api.completion:,} output"
            + (f", of which {cost.api.reasoning:,} reasoning" if cost.api.reasoning else "") + "."
        )
    if len(cost.jobs) > 1:
        top = sorted(cost.jobs.items(), key=lambda kv: -kv[1])[:5]
        lines.append("- By job: " + ", ".join(f"{job_id} {tokens:,}" for job_id, tokens in top) + ".")
    return "\n".join(lines)
