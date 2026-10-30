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

from . import __version__, actions, runner, runstate, worktree
from . import report as reporting
from .api import ApiKeyMissing, find_api_key
from .budget import Budgets
from .gate import Gate, GateConfig
from .store import DB_ENV, FINAL_STATUSES, Store

INSTRUCTIONS = (
    "spare-gpu queues coding chores (writing tests, adding docs, code review) for a git repo and "
    "does them in the background when BharatCode has spare capacity, each on its own git branch "
    "gtu/<job-id>, never touching the user's branch. Queueing a job starts the background runner "
    "if needed; it keeps going after BharatCode closes. Relay the tools' answers to the user as "
    "they are."
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

    def __init__(self, base: Path, make_gate: Callable[[Store], Gate] | None = None,
                 launch: Callable[[Path], int] | None = None):
        self.base = Path(base)
        # The gate reads BharatCode's public live stats; tests pass one wired to a fake.
        self._make_gate = make_gate or (lambda store: Gate(store, GateConfig.from_env()))
        # Starting the background runner; tests pass a stand-in.
        self._launch = launch or runner.launch

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
            return f"The runner (process {pid}) is running and will pick queued jobs up when there's capacity."
        return "The runner isn't running; spare_start starts it (or `gtu start` in a terminal)."

    def _ensure_runner(self, store: Store, repo: Path) -> str:
        """Start the background runner unless it's already going. Returns what to tell the user."""
        pid = runstate.running_pid(store.path.parent)
        if pid:
            return (f"The runner (process {pid}) is already running; it picks this up when BharatCode has "
                    "spare capacity.")
        try:
            if find_api_key() is None:
                return ("It can't run yet: no BharatCode API key is set up. Run `gtu setup` in a terminal, "
                        "then ask me to start spare-gpu.")
        except ApiKeyMissing as exc:
            return f"It can't run yet: {exc}. Run `gtu setup` in a terminal, then ask me to start spare-gpu."
        try:
            pid = self._launch(repo)
        except (runner.RunnerError, OSError) as exc:
            return f"Couldn't start the runner: {exc}"
        return (f"Started the runner in the background (process {pid}). It sends work only when BharatCode has "
                "spare capacity, keeps going after you close BharatCode, and stops by itself when the queue is "
                "empty. To stop it sooner: spare_stop, or `gtu stop` in a terminal.")

    @staticmethod
    def _when(stamp: str | None) -> str:
        if not stamp:
            return "-"
        return datetime.fromisoformat(stamp).astimezone().strftime("%d %b %H:%M")

    # --- tools -----------------------------------------------------------------------------

    def add_job(self, kind: str, path: str, note: str | None = None, start: bool = True) -> str:
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
            lines.append(self._ensure_runner(store, Path(job.repo)) if start else self._runner(store))
        return "\n".join(lines)

    def start(self) -> str:
        try:
            with self._open() as store:
                waiting = store.list_jobs(include_finished=False)
                if not waiting:
                    return "Nothing is queued, so there's nothing to start. Queue a job with spare_add_job."
                return f"{len(waiting)} job(s) waiting. " + self._ensure_runner(store, self._root())
        except NoStore as exc:
            return str(exc)

    def stop(self) -> str:
        try:
            with self._open() as store:
                lines = actions.stop_runner(store)
        except NoStore as exc:
            return str(exc)
        except actions.ActionError as exc:
            return f"Couldn't stop it: {exc}"
        if not lines[0].startswith("Nothing is running"):
            lines.append("Queued jobs stay queued; spare_start (or queueing another job) starts the runner again.")
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
        """Queue a coding chore for spare-gpu to do in the background on BharatCode's spare capacity:
        write tests, add documentation (docstrings, README), or write a code review, for a file or
        folder in this git repo. Use it when the user wants something done later, tonight, overnight
        or "when there's capacity", e.g. "queue tests for the payments module tonight", "get docs
        written for utils overnight", "review src/api later". It doesn't do the work in this chat: it
        queues the job and starts spare-gpu's background runner if it isn't running, which does the
        work on its own git branch gtu/<job-id> when BharatCode has a free slot.

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

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    def spare_start() -> str:
        """Start spare-gpu's background runner so queued jobs get done: it waits for BharatCode's
        spare capacity, works through the queue, keeps going after BharatCode closes, and stops by
        itself when the queue is empty. Use it for "start spare-gpu", "run my queued jobs", "resume".
        Queueing a job with spare_add_job already does this, so it's only needed after spare_stop."""
        return tools.start()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def spare_stop() -> str:
        """Stop spare-gpu's background runner now. Jobs in progress are paused, not lost: finished
        steps stay on their branches and spare_start resumes them. Use it for "stop spare-gpu",
        "pause the queue", "don't use any more tokens tonight"."""
        return tools.stop()

    return server


def serve(base: Path) -> None:
    build_server(base).run("stdio")
