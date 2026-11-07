"""Token totals for spare-gpu's work (`gtu report`), from the request log.

Every request we send is logged, and so is every step a job's agent reports. Per-session detail
(input, output, cached and reasoning tokens, and a breakdown by message type) is in BharatCode
Desktop itself: each job is a Desktop session named "spare-gpu: ...". What spare-gpu adds is the
total across jobs and days, next to what each job produced, and the budget that stops a job.
(An earlier "overhead vs task" split rested on a fixed per-step estimate and was dropped:
Desktop's own breakdown is measured.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import Store, StoreError

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
    all: Tally = field(default_factory=Tally)
    jobs: dict[str, Tally] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.all.total


def _job_or_none(store: Store, job_id: str):
    try:
        return store.get_job(job_id)
    except StoreError:
        return None


def collect(store: Store, scope: str | None, now: datetime | None = None) -> tuple[Cost, str]:
    """Tokens for a scope: a job id, "all", or a number of hours (default: the last 24).
    Returns the tally and a description of what it covers."""
    now = now or datetime.now(timezone.utc)
    scope = (scope or "").strip().lower()
    if scope in ("", "last_night", "last night", "today", "session", "tonight"):
        scope = str(DEFAULT_HOURS)
    # Job ids are 8 hex characters, so about 1 in 40 is all digits ("16170455"): a job by that id
    # wins over reading the number as hours.
    job = _job_or_none(store, scope) if scope != "all" else None
    if scope == "all":
        rows, label = store.conn.execute("SELECT * FROM requests").fetchall(), "all time"
    elif job is None and scope.replace(".", "", 1).isdigit():
        since = (now - timedelta(hours=float(scope))).isoformat()
        rows = store.conn.execute("SELECT * FROM requests WHERE sent_at >= ?", (since,)).fetchall()
        label = f"the last {float(scope):g} hours"
    else:
        job = job or store.get_job(scope)  # StoreError for an unknown id
        rows = store.requests(job.id)
        label = f"job {job.id} ({job.kind} {job.target})"
    cost = Cost()
    for row in rows:
        cost.all.add(row)
        if row["job_id"]:
            cost.jobs.setdefault(row["job_id"], Tally()).add(row)
    return cost, label
