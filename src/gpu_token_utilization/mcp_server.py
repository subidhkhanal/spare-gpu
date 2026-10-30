"""MCP server: spare-gpu's queue as tools an agent can call (BharatCode's CLI, or any MCP client).

Started with `gtu mcp`, over stdio. Each tool is a thin wrapper over the code the matching
`gtu` command uses, reading and writing the same .gtu/gtu.db; the store is safe to share with a
running `gtu start` (see Store). Tools return plain text, because the agent relays it to the user.
The server never runs jobs itself: `gtu start` does, when BharatCode has spare capacity.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from . import __version__, actions, runstate, worktree
from . import report as reporting
from .budget import Budgets
from .gate import Gate, GateConfig
from .store import DB_ENV, FINAL_STATUSES, Store

INSTRUCTIONS = (
    "spare-gpu queues coding chores (writing tests, adding docs, code review) for a git repo and "
    "runs them later on BharatCode's spare capacity, each on its own git branch gtu/<job-id>. "
    "Queueing a job doesn't do the work: the `gtu start` command runs queued jobs when there is "
    "capacity. Relay the tools' answers to the user as they are."
)


DEFAULT_REPORT_HOURS = 24


class NoStore(Exception):
    """There's no .gtu store for this project yet."""


def project_root(base: Path) -> Path:
    """The git repo containing `base`, or `base` itself outside a repo."""
    try:
        return worktree.repo_root(base).resolve()
    except worktree.GitError:
        return Path(base).resolve()


def db_path(root: Path) -> Path:
    override = os.environ.get(DB_ENV)
    return Path(override) if override else root / ".gtu" / "gtu.db"


class SpareTools:
    """The five tools as plain functions returning text, for one project folder."""

    def __init__(self, base: Path, make_gate: Callable[[Store], Gate] | None = None):
        self.base = Path(base)
        # The gate reads BharatCode's public live stats; tests pass one wired to a fake.
        self._make_gate = make_gate or (lambda store: Gate(store, GateConfig.from_env()))

    # --- helpers -------------------------------------------------------------------------

    def _root(self) -> Path:
        return project_root(self.base)

    def _open(self, create: bool = False) -> Store:
        path = db_path(self._root())
        if not create and not path.exists():
            raise NoStore(
                f"spare-gpu has no jobs for this project yet (no .gtu folder in {self._root()}). "
                "Queue one with spare_add_job, or run `gtu add` in a terminal in the repo."
            )
        return Store(path)

    @staticmethod
    def _runner(store: Store) -> str:
        pid = runstate.running_pid(store.path.parent)
        if pid:
            return f"The runner (`gtu start`, process {pid}) is running and will pick queued jobs up when there's capacity."
        return ("Nothing is running jobs right now. To run them, start `gtu start` in a terminal in this repo "
                "and leave it running; it waits for spare capacity by itself.")

    @staticmethod
    def _when(stamp: str | None) -> str:
        if not stamp:
            return "-"
        return datetime.fromisoformat(stamp).astimezone().strftime("%d %b %H:%M")

    # --- tools -----------------------------------------------------------------------------

    def add_job(self, kind: str, path: str, note: str | None = None) -> str:
        try:
            outcome = actions.queue_job(lambda repo: Store(db_path(repo)), self._root(), kind, path, note)
        except actions.ActionError as exc:
            return f"Didn't queue it: {exc}"
        job = outcome.job
        lines = [f"Queued a {job.kind} job for {job.target} (job id {job.id}). "
                 f"Its results will go on git branch {worktree.branch_name(job.id)}; your own branch isn't touched."]
        if outcome.note:
            lines.append(outcome.note)
        with Store(db_path(Path(job.repo))) as store:
            lines.append(self._runner(store))
        return "\n".join(lines)

    def list_jobs(self) -> str:
        try:
            with self._open() as store:
                jobs = store.list_jobs()
                tokens = {j.id: store.request_summary(j.id)["total_tokens"] for j in jobs}
                runner = self._runner(store)
        except NoStore as exc:
            return str(exc)
        if not jobs:
            return f"No jobs. {runner}"
        groups = [("Running", ("running",)), ("Waiting", ("queued", "paused")), ("Finished", FINAL_STATUSES)]
        lines = [f"{len(jobs)} job(s) in {self._root()}."]
        for title, statuses in groups:
            chosen = [j for j in jobs if j.status in statuses]
            if not chosen:
                continue
            lines.append(f"{title}:")
            for j in chosen:
                spent = f", {tokens[j.id]:,} tokens" if tokens[j.id] else ""
                lines.append(f"- {j.id}: {j.kind} {j.target}, {j.status} (since {self._when(j.updated_at)}){spent}")
        lines.append(runner)
        return "\n".join(lines)

    def report(self, hours: float | None = None) -> str:
        now = datetime.now(timezone.utc)
        budgets = Budgets.from_env()
        # Default: the last 24 hours, not "since tonight began" like `gtu report`. Asked at 13:00,
        # "what did it do last night?" means the night before, which began yesterday.
        since = now - timedelta(hours=hours or DEFAULT_REPORT_HOURS)
        try:
            with self._open() as store:
                reports = [reporting.build_job(store, j) for j in reporting.select_jobs(store, since, None)]
                night = store.tokens_since(budgets.night_began(now).isoformat())
        except NoStore as exc:
            return str(exc)
        period = f"since {reporting._local(since)}"
        if not reports:
            return f"Nothing ran {period}."
        total = sum(r.tokens for r in reports)
        lines = [f"spare-gpu {period}: {len(reports)} job(s), {total:,} tokens "
                 f"({night:,} of tonight's {budgets.per_night:,}-token budget used)."]
        for r in reports:
            j = r.job
            made = ", ".join(f"{p} ({ {'A': 'added', 'M': 'changed', 'D': 'deleted'}.get(c, c)})" for c, p in r.files)
            tests = (f" Tests: {r.tests_kept} kept, {r.tests_dropped} discarded as failing."
                     if r.tests_kept is not None else "")
            lines.append(
                f"- {j.kind} {j.target} (job {j.id}): {j.status}; started {reporting._local(r.started)}, "
                f"took {reporting._span(r.duration)}, {r.tokens:,} tokens. "
                f"Produced: {made or 'nothing committed'}.{tests}"
                + (f" See it with: git diff HEAD...{worktree.branch_name(j.id)}" if r.files else "")
            )
        waits = sorted(w for r in reports for w in r.queue_waits_ms)
        if waits:
            lines.append(f"BharatCode held these requests in its queue for {waits[len(waits) // 2] / 1000:.1f} s "
                         f"(median), {waits[-1] / 1000:.1f} s at most.")
        return "\n".join(lines)

    def why(self) -> str:
        try:
            with self._open() as store:
                gate = self._make_gate(store)
                try:
                    decision = gate.check(allow_probe=False)
                finally:
                    gate.close()
                waiting = store.list_jobs(include_finished=False)
                runner = self._runner(store)
        except NoStore as exc:
            return str(exc)
        lines = [decision.describe()]
        lines.append(f"{len(waiting)} job(s) waiting." if waiting else "No jobs are waiting.")
        lines.append(runner)
        return "\n".join(lines)

    def cancel(self, job_id: str) -> str:
        try:
            with self._open() as store:
                outcome = actions.cancel_job(store, job_id.strip())
        except NoStore as exc:
            return str(exc)
        except actions.ActionError as exc:
            return f"Didn't cancel it: {exc}. Use spare_list_jobs to see job ids and their status."
        job = outcome.job
        text = (f"Cancelled job {job.id} ({job.kind} {job.target}). Anything it already committed stays on "
                f"branch {worktree.branch_name(job.id)}.")
        return text + (f"\nNote: {outcome.note}" if outcome.note else "")


def build_server(base: Path, tools: SpareTools | None = None):
    """The MCP server for one project folder (the agent's working directory by default)."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tools = tools or SpareTools(base)
    server = MCPServer("spare-gpu", version=__version__, instructions=INSTRUCTIONS)
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def spare_add_job(type: Literal["tests", "docs", "review"], path: str, note: str | None = None) -> str:
        """Queue a coding chore for spare-gpu to do LATER on BharatCode's spare capacity: write tests,
        add documentation (docstrings, README), or write a code review, for a file or folder in this
        git repo. Use it when the user wants something done later, tonight, overnight or "when there's
        capacity", e.g. "queue tests for the payments module tonight", "get docs written for utils
        overnight", "review src/api later". It does not do the work now: the job runs when the
        `gtu start` runner is going, on its own git branch gtu/<job-id>.

        Args:
            type: "tests" to write new passing tests, "docs" to add docstrings/README text,
                "review" for a written code review (bugs, risky code, suggestions).
            path: File or folder relative to the repo root, e.g. "src/payments" or "app/models.py".
                If the user names a module loosely, find its real path first.
            note: Optional extra instructions for the job, in the user's words.
        """
        return tools.add_job(type, path, note)

    @server.tool(annotations=reads)
    def spare_list_jobs() -> str:
        """List spare-gpu's jobs for this repo: what's queued, running, paused, done, failed or
        cancelled, with job ids and tokens used, and whether the runner (`gtu start`) is going.
        Use it for "what's queued?", "is my tests job done?", "what is spare-gpu doing?", or to find
        a job id before cancelling."""
        return tools.list_jobs()

    @server.tool(annotations=reads)
    def spare_report(hours: float | None = None) -> str:
        """Summarise what spare-gpu did: which jobs ran and when, how long they took, tokens used,
        and what each produced (files on its branch, tests kept or discarded). Use it for "what did
        spare-gpu do last night?", "what got done?", "how many tokens did it use?".

        Args:
            hours: How many hours back to look. Leave it out for the last 24 hours, which covers
                "last night" and "today"; use e.g. 168 for "this week".
        """
        return tools.report(hours)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    def spare_why() -> str:
        """Say whether spare-gpu would send work to BharatCode right now, and why: its capacity
        gate's decision from BharatCode's live load (busy slots, queue length), how many jobs are
        waiting, and whether the runner is going. Use it for "why hasn't my job started?", "is
        BharatCode busy?", "when will my job run?". Sends no work."""
        return tools.why()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def spare_cancel(job_id: str) -> str:
        """Cancel a spare-gpu job that hasn't finished (queued, running or paused). Anything it
        already committed stays on its branch gtu/<job-id>. Use it for "cancel the docs job",
        "drop job 1a2b3c4d", "stop the review of utils".

        Args:
            job_id: The job's 8-character id. If the user describes the job instead, call
                spare_list_jobs first to find it.
        """
        return tools.cancel(job_id)

    return server


def serve(base: Path) -> None:
    build_server(base).run("stdio")
