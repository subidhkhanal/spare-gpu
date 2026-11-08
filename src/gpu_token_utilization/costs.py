"""Token totals for spare-gpu's work, from the request log.

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

from . import stats, waits
from .scheduler import kept_nothing
from .store import FINAL_STATUSES, Job, Store, StoreError

DEFAULT_HOURS = 24
DEFAULT_DAYS = 1
TOP_JOBS = 3


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


# --- /cost: a period's tokens from both sources ------------------------------------------------
# BharatCode Desktop's own record of every reply has the tokens per model for every session on this
# machine (spare-gpu's jobs are Desktop sessions too, so they are in there). spare-gpu's request log
# has what each job cost and whether its check kept anything. Neither has the other's view.


@dataclass
class JobCost:
    job: Job
    tokens: int
    kept_nothing: bool  # a done job whose verify step kept no test, doc change or fix

    @property
    def label(self) -> str:
        return f"{self.job.kind} {self.job.target} ({self.job.id})"

    @property
    def outcome(self) -> str:
        return f"{self.job.status}, kept nothing" if self.kept_nothing else self.job.status


@dataclass
class Period:
    days: float
    models: dict[str, dict[str, float]] | None  # None: no BharatCode Desktop data on this machine
    jobs: list[JobCost] = field(default_factory=list)
    channel: str = "stable"  # whose record the model figures come from

    @property
    def ran(self) -> int:
        return len(self.jobs)

    def count(self, status: str) -> int:
        return sum(1 for j in self.jobs if j.job.status == status)

    @property
    def tokens(self) -> int:
        return sum(j.tokens for j in self.jobs)

    @property
    def kept_nothing(self) -> int:
        return sum(1 for j in self.jobs if j.kept_nothing)

    @property
    def top(self) -> list[JobCost]:
        """The most expensive jobs, costliest first."""
        return sorted((j for j in self.jobs if j.tokens > 0), key=lambda j: (-j.tokens, j.job.created_at))[:TOP_JOBS]


def _kept_nothing(store: Store, job: Job) -> bool:
    if job.status != "done":
        return False
    return any(s["name"] == "verify" and s["status"] == "done" and kept_nothing(s["output"]) for s in store.steps(job.id))


def period(store: Store, days: float = DEFAULT_DAYS, now: datetime | None = None,
           usage: dict[str, dict[str, float]] | None | str = "ask", channel: str | None = None) -> Period:
    """The last `days` days: tokens per model from Desktop's own record of every reply, across all
    projects (None when there's no Desktop data), and spare-gpu's jobs that finished or sent a
    request in the period, each with the tokens of all its requests (what the job cost) and whether
    its check kept anything. `usage` is read from Desktop unless given (tests pass figures or None):
    from `channel`'s record (stable, beta, dev or local), else the Desktop that started us, else
    the released app's."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    channel = channel or stats.channel()
    if usage == "ask":
        db = stats.session_db(which=channel)
        models = stats.tokens_since(now - timedelta(days=days), db=db) if db else None
    else:
        models = usage
    active = {r["job_id"] for r in store.conn.execute(
        "SELECT DISTINCT job_id FROM requests WHERE job_id IS NOT NULL AND sent_at >= ?", (since,))}
    jobs: list[JobCost] = []
    for job in store.list_jobs():
        if job.id not in active and not (job.status in FINAL_STATUSES and job.updated_at >= since):
            continue
        tokens = sum(r["total_tokens"] or 0 for r in store.requests(job.id))
        jobs.append(JobCost(job, tokens, _kept_nothing(store, job)))
    return Period(days=days, models=models, jobs=jobs, channel=channel)


NO_DATA = "BharatCode Desktop's session data wasn't found on this machine, so no per-model figures"


def _no_replies(p: Period) -> str:
    return (f"BharatCode Desktop ({p.channel}) recorded no replies in this period; "
            "`/cost beta` (or stable, dev, local) reads another Desktop build.")
STATS_NOTE = "exact, from BharatCode Desktop's own record: every session on this machine"


def _window(days: float) -> str:
    if days == 1:
        return "the last 24 hours"
    return f"the last {days:g} days"


def _model_rows(p: Period) -> list[tuple[str, int, int]]:
    """(name, messages, tokens) per model, largest first."""
    rows = [(waits.name(model), int(f.get("messages", 0)), int(f.get("tokens", 0))) for model, f in (p.models or {}).items()]
    return sorted(rows, key=lambda r: (-r[2], r[0]))


def _job_counts(p: Period) -> str:
    parts = [f"{p.count(s)} {s}" for s in FINAL_STATUSES if p.count(s)]
    return f"{p.ran} job(s) ran" + (f" ({', '.join(parts)})" if parts else "")


def render(p: Period) -> str:
    """Per-model tokens, then the period's jobs: counts, tokens, how many kept nothing, and the
    three most expensive."""
    lines = [f"Cost for {_window(p.days)}", ""]
    if p.models is None:
        lines.append(f"{NO_DATA}.")
    elif not p.models:
        lines.append(_no_replies(p))
    else:
        lines.append(f"Tokens per model ({STATS_NOTE}):")
        rows = _model_rows(p)
        width = max(len("Model"), *(len(r[0]) for r in rows))
        lines.append(f"  {'Model':<{width}}  Messages      Tokens")
        for name, messages, tokens in rows:
            lines.append(f"  {name:<{width}}  {messages:>8}  {tokens:>10,}")
    lines.append("")
    if not p.jobs:
        lines.append(f"spare-gpu jobs: none ran in {_window(p.days)}.")
        return "\n".join(lines)
    lines.append(f"spare-gpu jobs: {_job_counts(p)}, {p.tokens:,} tokens; {p.kept_nothing} kept nothing after its check.")
    width = max(len("Job"), *(len(f"{j.job.kind} {j.job.target}") for j in p.jobs))
    lines.append(f"  {'Job':<{width}}  {'Id':<8}  {'Outcome':<20}  {'Tokens':>10}")
    for j in sorted(p.jobs, key=lambda j: j.job.created_at):
        lines.append(f"  {j.job.kind + ' ' + j.job.target:<{width}}  {j.job.id:<8}  {j.outcome:<20}  {j.tokens:>10,}")
    if p.top:
        lines.append("")
        lines.append("Most expensive:")
        for rank, j in enumerate(p.top, 1):
            lines.append(f"  {rank}. {j.label}: {j.tokens:,} tokens, {j.outcome}")
    return "\n".join(lines)


def render_short(p: Period) -> str:
    """One paragraph, for a chat command to relay."""
    window = _window(p.days)
    if p.models is None:
        models = f"{NO_DATA}."
    elif not p.models:
        models = _no_replies(p)
    else:
        used = "; ".join(f"{name} {tokens:,} tokens in {messages} message(s)" for name, messages, tokens in _model_rows(p))
        models = f"By model ({STATS_NOTE}): {used}."
    if not p.jobs:
        jobs = f"spare-gpu ran no jobs in {window}."
    else:
        jobs = f"spare-gpu: {_job_counts(p)} for {p.tokens:,} tokens, and {p.kept_nothing} kept nothing after its check."
        if p.top:
            jobs += " Most expensive: " + "; ".join(f"{j.label} {j.tokens:,} tokens, {j.outcome}" for j in p.top) + "."
    return f"In {window}: {models} {jobs}"
