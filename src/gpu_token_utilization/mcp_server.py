"""MCP server: spare-gpu as tools an agent can call (BharatCode's CLI, or any MCP client).

Started with `gtu mcp`, over stdio. Seven tools, each shaped around a task the user wants done
rather than around our internal functions. Selection accuracy drops as tool count grows and when
tools overlap, so related questions are merged (one status tool answers "what's queued", "is it
done" and "why isn't it running"). Each tool is a thin wrapper over the code the matching `gtu`
command uses, sharing the repo's .gtu/gtu.db (safe alongside a running `gtu start`). Tools return
plain text for the agent to relay, and none blocks: long work is handed to a background process.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from . import __version__, actions, advice, costs, freerun, runner, runstate, worktree
from . import report as reporting
from .api import ApiKeyMissing, find_api_key
from .budget import Budgets
from .gate import Gate, GateConfig
from .store import DB_ENV, FINAL_STATUSES, Command, Store, StoreError

INSTRUCTIONS = (
    "spare-gpu does coding chores (writing tests, adding docs, code review) for a git repo, "
    "unattended in the background, each on its own git branch gtu/<job-id>, never touching the "
    "user's branch, and keeps going after BharatCode closes. It also knows BharatCode's load, so it "
    "can say whether now is a busy time and run any command once there's a free slot. Relay the "
    "tools' answers to the user as they are."
)
LAST_NIGHT_HOURS = 24


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
    """The seven tools as plain functions returning text, for one project folder."""

    def __init__(self, base: Path, make_gate: Callable[[Store], Gate] | None = None,
                 launch: Callable[[Path], int] | None = None,
                 start_command: Callable[[Command], int] | None = None):
        self.base = Path(base)
        # Everything that reaches outside is injectable, so tests use fakes: the gate (BharatCode's
        # live stats), the background job runner and the run-when-free waiter.
        self._make_gate = make_gate or (lambda store: Gate(store, GateConfig.from_env()))
        self._launch = launch or runner.launch
        self._start_command = start_command

    # --- helpers -------------------------------------------------------------------------

    def _root(self) -> Path:
        return project_root(self.base)

    def _open(self, create: bool = False) -> Store:
        path = db_path(self._root())
        if not create and not path.exists():
            raise NoStore(
                f"spare-gpu hasn't been used in this project yet (no .gtu folder in {self._root()}). "
                "Queue something with spare_queue_work first."
            )
        return Store(path)

    @staticmethod
    def _when(stamp: str | None) -> str:
        if not stamp:
            return "-"
        return datetime.fromisoformat(stamp).astimezone().strftime("%d %b %H:%M")

    def _decision(self, store: Store):
        gate = self._make_gate(store)
        try:
            return gate.check(allow_probe=False)
        finally:
            gate.close()

    def _ensure_runner(self, store: Store, repo: Path) -> str:
        """Start the background runner unless it's already going. Returns what to tell the user."""
        pid = runstate.running_pid(store.path.parent)
        if pid:
            return f"spare-gpu's runner (process {pid}) is already going; it picks this up next."
        try:
            if find_api_key() is None:
                return "It can't run yet: no BharatCode API key is set up. Run `gtu setup` in a terminal."
        except ApiKeyMissing as exc:
            return f"It can't run yet: {exc}. Run `gtu setup` in a terminal."
        try:
            pid = self._launch(repo)
        except (runner.RunnerError, OSError) as exc:
            return f"Couldn't start the background runner: {exc}"
        return (f"Started spare-gpu's runner in the background (process {pid}). It starts now, waits politely if "
                "BharatCode says busy, keeps going after you close BharatCode, and stops when the queue is empty.")

    # --- the seven tools -------------------------------------------------------------------

    def queue_work(self, kind: str, path: str, note: str | None = None, start: bool = True) -> str:
        try:
            outcome = actions.queue_job(lambda repo: Store(db_path(repo)), self._root(), kind, path, note)
        except actions.ActionError as exc:
            return f"Didn't queue it: {exc}"
        job = outcome.job
        lines = [f"Queued a {job.kind} job for {job.target} (job {job.id}). Results go on git branch "
                 f"{worktree.branch_name(job.id)}; your own branch isn't touched."]
        if outcome.note:
            lines.append(outcome.note)
        with Store(db_path(Path(job.repo))) as store:
            if start:
                lines.append(self._ensure_runner(store, Path(job.repo)))
        return "\n".join(lines)

    def work_status(self) -> str:
        try:
            with self._open() as store:
                jobs = store.list_jobs()
                commands = store.list_commands()
                tokens = {j.id: store.request_summary(j.id)["total_tokens"] for j in jobs}
                pid = runstate.running_pid(store.path.parent)
                waiting = [j for j in jobs if j.status in ("queued", "paused")]
                decision = self._decision(store) if waiting or any(c.status == "waiting" for c in commands) else None
        except NoStore as exc:
            return str(exc)
        if not jobs and not commands:
            return f"Nothing queued, running or finished yet in {self._root()}."
        # Named so a wrong folder is obvious (BharatCode can start the server outside the repo).
        lines = [f"spare-gpu's work in {self._root()}:"]
        for title, statuses in (("Running", ("running",)), ("Waiting", ("queued", "paused")),
                                ("Finished", FINAL_STATUSES)):
            chosen = [j for j in jobs if j.status in statuses]
            if chosen:
                lines.append(f"{title}:")
                for j in chosen:
                    spent = f", {tokens[j.id]:,} tokens" if tokens[j.id] else ""
                    lines.append(f"- {j.id}: {j.kind} {j.target}, {j.status} since {self._when(j.updated_at)}{spent}")
        if commands:
            lines.append("Commands to run when there's capacity:")
            for c in commands[-10:]:
                result = f", exit code {c.exit_code}" if c.exit_code is not None else ""
                lines.append(f"- {c.id}: `{c.command}`, {c.status}{result} (log: {c.log_path})")
        if pid:
            lines.append(f"spare-gpu's runner is going (process {pid}).")
        elif waiting:
            lines.append("spare-gpu's runner isn't going, so waiting jobs won't start; queueing another job "
                         "starts it, or run `gtu start`.")
        if decision is not None:
            lines.append(("Capacity: " if decision.open else "Why it isn't running yet: ") + decision.describe())
        return "\n".join(lines)

    def last_night(self, hours: float | None = None) -> str:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours or LAST_NIGHT_HOURS)
        budgets = Budgets.from_env()
        try:
            with self._open() as store:
                reports = [reporting.build_job(store, j) for j in reporting.select_jobs(store, since, None)]
                cost, _ = costs.collect(store, str(hours or LAST_NIGHT_HOURS), now)
                night = store.tokens_since(budgets.night_began(now).isoformat())
        except NoStore as exc:
            return str(exc)
        period = f"since {reporting._local(since)}"
        if not reports:
            return f"Nothing ran {period}."
        lines = [f"spare-gpu {period}: {len(reports)} job(s), {cost.total:,} tokens "
                 f"({night:,} of tonight's {budgets.per_night:,}-token budget used)."]
        for r in reports:
            j = r.job
            made = ", ".join(f"{p} ({ {'A': 'added', 'M': 'changed', 'D': 'deleted'}.get(c, c)})" for c, p in r.files)
            tests = (f" Tests: {r.tests_kept} kept, {r.tests_dropped} discarded as failing."
                     if r.tests_kept is not None else "")
            lines.append(
                f"- {j.kind} {j.target} (job {j.id}): {j.status}; started {reporting._local(r.started)}, "
                f"took {reporting._span(r.duration)}, {r.tokens:,} tokens. Produced: {made or 'nothing committed'}."
                + tests + (f" See it with: git diff HEAD...{worktree.branch_name(j.id)}" if r.files else "")
            )
        waits = sorted(w for r in reports for w in r.queue_waits_ms)
        if waits:
            lines.append(f"BharatCode held these requests in its queue for {waits[len(waits) // 2] / 1000:.1f} s "
                         f"(median), {waits[-1] / 1000:.1f} s at most.")
        return "\n".join(lines)

    def cancel_work(self, work_id: str) -> str:
        work_id = work_id.strip()
        try:
            with self._open() as store:
                try:
                    store.get_command(work_id)
                except StoreError:
                    outcome = actions.cancel_job(store, work_id)
                else:
                    cmd = freerun.cancel(store, work_id)
                    return f"Cancelled command {cmd.id} (`{cmd.command}`)."
        except NoStore as exc:
            return str(exc)
        except (actions.ActionError, StoreError) as exc:
            return f"Didn't cancel it: {exc}. spare_work_status lists ids and what's still running."
        job = outcome.job
        text = (f"Cancelled job {job.id} ({job.kind} {job.target}). Anything it already committed stays on "
                f"branch {worktree.branch_name(job.id)}.")
        return text + (f"\nNote: {outcome.note}" if outcome.note else "")

    def capacity_advice(self) -> str:
        path = db_path(self._root())
        with Store(path if path.exists() else ":memory:") as store:  # advice needs no jobs
            decision = self._decision(store)
            waits = advice.measured_waits(store)
        return advice.advise(waits, decision)

    def token_cost(self, scope: str | None = None) -> str:
        try:
            with self._open() as store:
                return costs.describe(store, scope)
        except NoStore as exc:
            return str(exc)

    def run_when_free(self, command: str) -> str:
        try:
            cmd = freerun.queue(lambda repo: Store(db_path(repo)), self._root(), command, self._start_command)
        except freerun.FreeRunError as exc:
            return f"Didn't queue it: {exc}."
        except OSError as exc:
            return f"Couldn't start waiting for capacity: {exc}"
        return (f"Will run `{cmd.command}` in {cmd.repo} as soon as BharatCode has spare capacity (command {cmd.id}). "
                f"It runs in the background with Command Prompt; output goes to {cmd.log_path}. "
                "spare_work_status shows when it has run; spare_cancel_work stops it.")


def build_server(base: Path, tools: SpareTools | None = None):
    """The MCP server for one project folder (the agent's working directory by default)."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tools = tools or SpareTools(base)
    server = MCPServer("spare-gpu", version=__version__, instructions=INSTRUCTIONS)

    def notes(read_only: bool, destructive: bool = False, world: bool = False) -> ToolAnnotations:
        return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive, open_world_hint=world)

    @server.tool(annotations=notes(False))
    def spare_queue_work(type: Literal["tests", "docs", "review"], path: str, note: str | None = None) -> str:
        """Queue a coding chore for spare-gpu to do in the background: write tests, write
        documentation, or review code, for a file or folder in this git repo. The work happens
        in the background, unattended, on its own git branch; it doesn't happen in this chat.

        Use it when the user wants tests, docs or a review done in the background, later, tonight
        or overnight. Examples: "queue tests for the payments module", "write tests for
        src/payments tonight", "get docs written for utils overnight", "queue a code review of
        app/api.py", "review app/api.py later". Call it straight away: it is the whole job, and it
        checks the path itself.
        For any other command (a build, a migration, a big refactor script) use spare_run_when_free.

        Args:
            type: "tests" writes new passing tests; "docs" adds docstrings and README text without
                changing code; "review" writes a code review (bugs, risky code, suggestions).
            path: File or folder relative to the repo root, e.g. "src/payments". If the user gave a
                path, pass it exactly as given without searching; spare-gpu says if it doesn't exist.
                Only search when the user named a module loosely ("the payments module").
            note: Optional extra instructions in the user's words, e.g. "cover refunds".
        """
        return tools.queue_work(type, path, note)

    @server.tool(annotations=notes(True, world=True))
    def spare_work_status() -> str:
        """Everything about spare-gpu's work in this repo in one answer: what's queued, what's
        running, what finished (with job ids and tokens), and, if something is waiting, why it
        isn't running yet (BharatCode's current load).

        Use it for any "where are things" question: "what's queued?", "is my tests job done?",
        "what is spare-gpu doing?", "why hasn't my job started?", "did my command run?". Also use it
        to find a job id before cancelling. For a summary of finished work use spare_last_night;
        for whether to start something new use spare_capacity_advice."""
        return tools.work_status()

    @server.tool(annotations=notes(True))
    def spare_last_night(hours: float | None = None) -> str:
        """What spare-gpu got done recently: which jobs ran, when, how long, what each produced
        (files on its branch, tests kept or discarded) and what it cost in tokens.

        Use it for a summary of results: "what did spare-gpu do last night?", "what got done?",
        "show me the overnight results". For one job's state ("is my tests job done?") or work still
        queued or running, use spare_work_status; for a token breakdown, spare_token_cost.

        Args:
            hours: How far back to look. Leave it out for the last 24 hours ("last night", "today");
                168 for "this week".
        """
        return tools.last_night(hours)

    @server.tool(annotations=notes(False, destructive=True))
    def spare_cancel_work(job_id: str) -> str:
        """Cancel a queued or running spare-gpu job, or a command waiting in spare_run_when_free.
        Anything a job already committed stays on its branch.

        Use it for "cancel the docs job", "drop job 1a2b3c4d", "don't run that command after all",
        "stop the tests for payments".

        Args:
            job_id: The 8-character id of the job or command. If the user describes it instead,
                call spare_work_status first to find the id.
        """
        return tools.cancel_work(job_id)

    @server.tool(annotations=notes(True, world=True))
    def spare_capacity_advice() -> str:
        """Is now a fine time for something heavy on BharatCode? Answers from how long our own
        requests actually waited in BharatCode's queue: the last few requests and whether waits are
        rising or falling, what's usual at this hour, and BharatCode's live load for context. It
        describes what was measured and says so when there isn't enough data; it doesn't forecast.

        Use it before starting anything big, or when the user asks about timing: "is now a good
        time to run a big refactor?", "is BharatCode busy right now?", "when is the quietest time
        to run tests?", "should I wait before kicking off this migration?". It only advises; to
        actually wait and run something, use spare_run_when_free or spare_queue_work. For why an
        already-queued job hasn't started, use spare_work_status."""
        return tools.capacity_advice()

    @server.tool(annotations=notes(True))
    def spare_token_cost(scope: str | None = None) -> str:
        """What spare-gpu's work actually consumed in tokens, split into BharatCode CLI overhead
        (the input the CLI re-sends on every step, mostly its own system prompt) and the work
        itself, with the biggest jobs listed.

        Use it when the question is about tokens or cost rather than results: "how many tokens did
        that cost?", "what did last night cost?", "how much of it was CLI overhead?", "how expensive
        was job 1a2b3c4d?". For what the work produced, use spare_last_night.

        Args:
            scope: A job id for one job; a number of hours, e.g. "24" (the default, also for "last
                night" or "today") or "168" for a week; or "all".
        """
        return tools.token_cost(scope)

    @server.tool(annotations=notes(False, world=True))
    def spare_run_when_free(command: str) -> str:
        """Run one existing command in this repo as soon as BharatCode has spare capacity, in the
        background. Returns at once with an id; the command waits, runs, and its output is logged.

        Use it to defer heavy commands, not to write tests/docs/reviews (that's spare_queue_work):
        "run the test suite when BharatCode is free", "kick off `bharatcode run 'add type hints'`
        tonight", "run npm run build once there's capacity". Only test, build and lint tools and
        `bharatcode run` are accepted (pytest, npm test, npm run <script>, go test, cargo test,
        make, ...); anything else, or anything with &&, pipes or redirects, is refused with the list.

        Args:
            command: One plain command line, e.g. "pytest -x" or "bharatcode run 'add docstrings'".
                Run directly in the repo root, not through a shell.
        """
        return tools.run_when_free(command)

    return server


def serve(base: Path) -> None:
    build_server(base).run("stdio")
