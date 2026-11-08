"""Token budgets: per job and per night. Reaching one pauses work; it never fails a job.

A "night" is the 24 hours starting at GTU_NIGHT_START (IST, default 12:00), so one
overnight session counts as one night even though it crosses midnight.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .store import Store

IST = timezone(timedelta(hours=5, minutes=30))


class BudgetExceeded(Exception):
    """Running (or continuing) this step would go over a token budget."""


@dataclass
class Budgets:
    # Each step of a job's Desktop session costs ~11k tokens (mostly BharatCode's own system prompt), so a
    # tests or docs job typically uses 50-150k.
    per_job: int = 200_000
    per_night: int = 1_000_000
    night_start: time = time(12, 0)

    @classmethod
    def from_env(cls) -> Budgets:
        b = cls()
        if os.environ.get("GTU_JOB_TOKEN_BUDGET"):
            b.per_job = int(os.environ["GTU_JOB_TOKEN_BUDGET"])
        if os.environ.get("GTU_NIGHT_TOKEN_BUDGET"):
            b.per_night = int(os.environ["GTU_NIGHT_TOKEN_BUDGET"])
        if os.environ.get("GTU_NIGHT_START"):
            b.night_start = time.fromisoformat(os.environ["GTU_NIGHT_START"])
        return b

    def night_began(self, now: datetime) -> datetime:
        """UTC start of the night that `now` falls in."""
        local = now.astimezone(IST)
        start = datetime.combine(local.date(), self.night_start, tzinfo=IST)
        if local < start:
            start -= timedelta(days=1)
        return start.astimezone(timezone.utc)

    def used(self, store: Store, job_id: str, now: datetime) -> tuple[int, int]:
        """(tokens used by this job, tokens used tonight across all jobs)."""
        job = store.request_summary(job_id)["total_tokens"]
        night = store.tokens_since(self.night_began(now).isoformat())
        return job, night

    def remaining(self, store: Store, job_id: str, now: datetime) -> int:
        job, night = self.used(store, job_id, now)
        return max(0, min(self.per_job - job, self.per_night - night))
