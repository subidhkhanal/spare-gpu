"""`gtu report`: the morning summary.

For each job: what ran and when, how long it took, tokens, what it produced, and the
capacity it was sent into (queue depth and busy slots at send time, BharatCode's reported
queue wait, and the share of sends that went into a free slot).

The overall summary is printed and saved under .gtu/reports/. Each finished job also gets
gtu-report-<job-id>.md committed to its branch, without checking anything out. Job reports
contain no "generated at" time, so running the report twice doesn't add a second commit.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import worktree
from .budget import IST, Budgets
from .store import FINAL_STATUSES, Job, Store

CHAT = "/chat/completions"
CLI = "bharatcode-cli"


@dataclass
class JobReport:
    job: Job
    started: datetime | None
    finished: datetime | None
    tokens: int
    api_requests: int
    cli_steps: int
    files: list[tuple[str, str]] = field(default_factory=list)  # (A/M/D, path) on the branch
    tests_kept: int | None = None
    tests_dropped: int | None = None
    reverted: int = 0
    sends_with_stats: int = 0
    sends_into_free_slot: int = 0
    queue_depths: list[int] = field(default_factory=list)
    queue_waits_ms: list[int] = field(default_factory=list)
    busy_answers: int = 0
    branch_exists: bool = False

    @property
    def duration(self) -> timedelta | None:
        return self.finished - self.started if self.started and self.finished else None

    @property
    def free_slot_share(self) -> float | None:
        return self.sends_into_free_slot / self.sends_with_stats if self.sends_with_stats else None


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str | None:
    proc = subprocess.run([*worktree.GIT, *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _branch_files(job: Job) -> tuple[bool, list[tuple[str, str]]]:
    repo, branch = Path(job.repo), worktree.branch_name(job.id)
    if not Path(job.repo).exists() or _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}") is None:
        return False, []
    base = job.base_commit or _git(repo, "merge-base", branch, "HEAD")
    if not base:
        return True, []
    out = _git(repo, "diff", "--name-status", base, branch) or ""
    files = [(line.split("\t")[0][0], line.split("\t")[-1]) for line in out.splitlines() if "\t" in line]
    return True, [f for f in files if not f[1].startswith("gtu-report-")]


def build_job(store: Store, job: Job) -> JobReport:
    steps = store.steps(job.id)
    started = [_ts(s["started_at"]) for s in steps if s["started_at"]]
    finished = [_ts(s["finished_at"]) for s in steps if s["finished_at"]]
    rows = store.requests(job.id)
    report = JobReport(
        job=job,
        started=min(started) if started else None,
        finished=max(finished) if finished and job.status in FINAL_STATUSES else None,
        tokens=sum(r["total_tokens"] or 0 for r in rows),
        api_requests=sum(1 for r in rows if r["endpoint"] == CHAT),
        cli_steps=sum(1 for r in rows if r["endpoint"] == CLI),
    )
    for r in rows:
        if r["status_code"] in (429, 503):
            report.busy_answers += 1
        if r["queue_wait_ms"] is not None:
            report.queue_waits_ms.append(r["queue_wait_ms"])
        if r["active_at_send"] is not None and r["slots_at_send"]:
            report.sends_with_stats += 1
            report.sends_into_free_slot += r["active_at_send"] < r["slots_at_send"]
        if r["queue_at_send"] is not None:
            report.queue_depths.append(r["queue_at_send"])
    for s in steps:
        output = s["output"] or ""
        if m := re.search(r"Kept (\d+) passing test\(s\), dropped (\d+)", output):
            report.tests_kept, report.tests_dropped = int(m[1]), int(m[2])
        report.reverted += len(re.findall(r"^- reverted ", output, flags=re.M))
    report.branch_exists, report.files = _branch_files(job)
    return report


def _local(t: datetime | None) -> str:
    return t.astimezone(IST).strftime("%d %b %H:%M IST") if t else "-"


def _span(d: timedelta | None) -> str:
    if d is None:
        return "-"
    minutes, seconds = divmod(int(d.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min {seconds} s"


def _waits(ms: list[int]) -> str:
    if not ms:
        return "not reported (BharatCode CLI requests don't expose it)"
    s = sorted(ms)
    return f"median {statistics.median(s) / 1000:.1f} s, max {s[-1] / 1000:.1f} s over {len(s)} request(s)"


def capacity_lines(r: JobReport) -> list[str]:
    lines = []
    if r.queue_depths:
        lines.append(f"- Queue at send: median {statistics.median(r.queue_depths):g}, max {max(r.queue_depths)}")
    lines.append(f"- BharatCode's reported queue wait: {_waits(r.queue_waits_ms)}")
    if r.free_slot_share is not None:
        lines.append(f"- Sent into a free slot: {r.sends_into_free_slot} of {r.sends_with_stats} "
                     f"({100 * r.free_slot_share:.0f}%)")
    if r.busy_answers:
        lines.append(f"- Busy answers (503/429): {r.busy_answers}")
    return lines


def job_markdown(r: JobReport) -> str:
    """The report committed to the job's branch. Deterministic: same job state, same text."""
    j = r.job
    lines = [f"# spare-gpu report: {j.kind} `{j.target}` (job `{j.id}`)", "",
             f"- Status: **{j.status}**",
             f"- Ran: {_local(r.started)} to {_local(r.finished)} ({_span(r.duration)}, including waiting for capacity)",
             f"- Tokens: {r.tokens:,} ({r.api_requests} API request(s), {r.cli_steps} BharatCode CLI step(s))"]
    if r.tests_kept is not None:
        lines.append(f"- Tests: kept {r.tests_kept} passing, discarded {r.tests_dropped} failing")
    if r.reverted:
        lines.append(f"- Changes reverted by our checks: {r.reverted}")
    lines += ["", "## Produced", ""]
    lines += [f"- `{path}` ({ {'A': 'added', 'M': 'changed', 'D': 'deleted'}.get(code, code)})" for code, path in r.files] \
        or ["- Nothing was committed."]
    lines += ["", "## Capacity it was sent into", ""] + capacity_lines(r)
    return "\n".join(lines) + "\n"


def write_to_branch(r: JobReport) -> str | None:
    """Commit the job's report to its branch if the job is finished and has one.

    Returns the new commit, "unchanged" if the branch already has this exact report, or None
    if there is nothing to write to (job still running, or no branch)."""
    j = r.job
    if j.status not in FINAL_STATUSES or not r.branch_exists:
        return None
    if worktree.worktree_path(Path(j.repo), j.id).exists():
        return None  # a live worktree has the branch checked out; don't move it underneath
    commit = worktree.add_file_to_branch(
        Path(j.repo), worktree.branch_name(j.id), f"gtu-report-{j.id}.md", job_markdown(r),
        f"gtu report for job {j.id}",
    )
    return commit or "unchanged"


def select_jobs(store: Store, since: datetime | None, job_id: str | None) -> list[Job]:
    if job_id:
        return [store.get_job(job_id)]
    jobs = []
    for job in store.list_jobs():
        activity = [_ts(job.updated_at)] + [_ts(s["finished_at"] or s["started_at"]) for s in store.steps(job.id)]
        if since is None or any(t and t >= since for t in activity):
            jobs.append(job)
    return jobs


def summary_markdown(reports: list[JobReport], store: Store, budgets: Budgets, now: datetime,
                     since: datetime | None, written: dict[str, str | None]) -> str:
    period = f"since {_local(since)}" if since else "all time"
    out = [f"# spare-gpu morning report ({period})", ""]
    if not reports:
        return "\n".join(out + ["Nothing ran in this period."]) + "\n"
    out += ["| Job | Kind | Target | Status | Ran | Took | Tokens | Produced |", "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        produced = f"{len(r.files)} file(s)"
        if r.tests_kept is not None:
            produced += f"; {r.tests_kept} tests kept, {r.tests_dropped} discarded"
        out.append(f"| `{r.job.id}` | {r.job.kind} | `{r.job.target}` | {r.job.status} | {_local(r.started)} "
                   f"| {_span(r.duration)} | {r.tokens:,} | {produced} |")
    total = sum(r.tokens for r in reports)
    night = store.tokens_since(budgets.night_began(now).isoformat())
    out += ["", f"**Tokens:** {total:,} across these jobs; {night:,} of {budgets.per_night:,} tonight's budget used.", ""]

    merged = JobReport(job=reports[0].job, started=None, finished=None, tokens=total, api_requests=0, cli_steps=0)
    for r in reports:
        merged.queue_depths += r.queue_depths
        merged.queue_waits_ms += r.queue_waits_ms
        merged.sends_with_stats += r.sends_with_stats
        merged.sends_into_free_slot += r.sends_into_free_slot
        merged.busy_answers += r.busy_answers
    out += ["## Capacity context", ""] + capacity_lines(merged) + [""]

    out += ["## Per job", ""]
    for r in reports:
        j = r.job
        out.append(f"### `{j.id}`: {j.kind} `{j.target}` ({j.status})")
        out += [f"- `{path}` ({ {'A': 'added', 'M': 'changed', 'D': 'deleted'}.get(code, code)})" for code, path in r.files] \
            or ["- Nothing committed."]
        if written.get(j.id):
            out.append(f"- Report committed to `{worktree.branch_name(j.id)}` as `gtu-report-{j.id}.md`.")
        elif j.status not in FINAL_STATUSES:
            out.append("- Still in progress; its branch report is written when it finishes.")
        if r.branch_exists:
            out.append(f"- Review it: `git log {worktree.branch_name(j.id)}`, `git diff HEAD...{worktree.branch_name(j.id)}`")
        out.append("")
    return "\n".join(out)
