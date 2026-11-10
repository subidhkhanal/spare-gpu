"""The token budget per job. Reaching it pauses the job; it never fails it."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from .store import Store

IST = timezone(timedelta(hours=5, minutes=30))

BUDGET_ENV = "GTU_JOB_TOKEN_BUDGET"


class BudgetExceeded(Exception):
    """Running (or continuing) this step would go over the job's token budget."""


@dataclass
class Budgets:
    # Each step of a job's Desktop session costs ~11k tokens (mostly BharatCode's own system prompt), so a
    # job typically uses 50-150k.
    per_job: int = 200_000

    @classmethod
    def load(cls, root: Path | None) -> Budgets:
        """The default, then the project's settings file (`root` None: no file), then GTU_JOB_TOKEN_BUDGET."""
        from . import settings

        b = cls()
        budget = settings.load(root).get("job_token_budget")
        if budget is not None:
            b.per_job = int(budget)
        if os.environ.get(BUDGET_ENV):
            b.per_job = int(os.environ[BUDGET_ENV])
        return b

    @classmethod
    def from_env(cls) -> Budgets:
        return cls.load(None)

    def used(self, store: Store, job_id: str) -> int:
        """Tokens this job has used so far, across every step and attempt."""
        return store.request_summary(job_id)["total_tokens"]

    def remaining(self, store: Store, job_id: str) -> int:
        return max(0, self.per_job - self.used(store, job_id))
