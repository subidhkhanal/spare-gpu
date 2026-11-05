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

import httpx

from . import __version__, actions, costs, desktop, freerun, router, runner, runstate, waits, worktree
from . import report as reporting
from .api import ApiKeyMissing, find_api_key
from .budget import Budgets
from .gate import Gate, GateConfig
from .store import DB_ENV, FINAL_STATUSES, Command, Store, StoreError

INSTRUCTIONS = (
    "spare-gpu does coding chores (writing tests, adding docs, code review) for a git repo as "
    "background BharatCode Desktop sessions, never touching the user's checkout: each finished job's "
    "edited files are copied to the project's spare-gpu/<date>_<time>/ folder. It measures how long each model would wait right now, and can hand a "
    "switch-safe task to whichever model starts it sooner, checking the result before keeping it. "
    "Relay the tools' answers to the user as they are."
)
LAST_NIGHT_HOURS = 24
# Desktop already shows each session's tokens in detail; spare-gpu points there rather than estimate.
SESSION_DETAIL = ("Each job runs as a BharatCode Desktop session named \"spare-gpu: ...\"; open it in Desktop "
                  "for its full token breakdown.")


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


QUESTION = "question"  # the router's "kind" for spare_ask
ASK_TIMEOUT = 600.0
ASK_DIR_ENV = "GTU_ASK_DIR"


def ask_folder() -> Path:
    """Where spare_ask's Desktop sessions run: spare-gpu's own folder, so a question never writes
    into the user's repo. Desktop lists these sessions under that folder."""
    override = os.environ.get(ASK_DIR_ENV)
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "spare-gpu" / "ask"
    base.mkdir(parents=True, exist_ok=True)
    return base


class SpareTools:
    """The eight tools as plain functions returning text, for one project folder."""

    def __init__(self, base: Path, make_gate: Callable[[Store], Gate] | None = None,
                 launch: Callable[[Path], int] | None = None,
                 start_command: Callable[[Command], int] | None = None,
                 http: Callable[[], httpx.Client] | None = None):
        self.base = Path(base)
        # Everything that reaches outside is injectable, so tests use fakes: the gate (BharatCode's
        # live stats), the background job runner, the run-when-free waiter, and the wait figures.
        self._make_gate = make_gate or (lambda store: Gate(store, GateConfig.from_env()))
        self._launch = launch or runner.launch
        self._start_command = start_command
        self._http = http or (lambda: httpx.Client(timeout=10.0))

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
            # Inside Desktop the runner works through Desktop's server and sign-in; it's detached
            # and can't find that server itself, so pass it on. No API key is needed then.
            inside = desktop.discover()
            desktop.export_url(inside)
        except desktop.DesktopNotFound:
            inside = None
        if inside is None:
            try:
                if find_api_key() is None:
                    return "It can't run yet: no BharatCode API key is set up. Run `gtu setup` in a terminal."
            except ApiKeyMissing as exc:
                return f"It can't run yet: {exc}. Run `gtu setup` in a terminal."
        try:
            pid = self._launch(repo)
        except (runner.RunnerError, OSError) as exc:
            return f"Couldn't start the background runner: {exc}"
        if inside is not None:
            return (f"Started spare-gpu's runner in the background (process {pid}). Each job runs as its own "
                    "BharatCode Desktop session, named \"spare-gpu: ...\", while Desktop is open; it stops when the "
                    "queue is empty.")
        return (f"Started spare-gpu's runner in the background (process {pid}). It starts now, waits politely if "
                "BharatCode says busy, keeps going after you close BharatCode, and stops when the queue is empty.")

    # --- the eight tools -------------------------------------------------------------------

    def queue_work(self, kind: str, path: str, note: str | None = None, start: bool = True) -> str:
        try:
            outcome = actions.queue_job(lambda repo: Store(db_path(repo)), self._root(), kind, path, note)
        except actions.ActionError as exc:
            return f"Didn't queue it: {exc}"
        job = outcome.job
        lines = [f"Queued a {job.kind} job for {job.target} (job {job.id}). When it finishes, its edited "
                 f"files are copied to {worktree.OUTPUT_DIR}/<date>_<time>/ in this project; your files aren't touched."]
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
                    on = f", on {waits.name(j.model)}" if j.model else ""
                    if j.model and j.preferred_model and waits.model_id(j.model) != waits.model_id(j.preferred_model):
                        on += f" instead of {waits.name(j.preferred_model)} (shorter wait)"
                    if j.fallback:
                        on += " (fell back after a failed check)"
                    files = f". Edited files: {j.output_dir}/" if j.output_dir else ""
                    lines.append(f"- {j.id}: {j.kind} {j.target}, {j.status} since {self._when(j.updated_at)}{spent}{on}{files}")
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
        if jobs:
            lines.append(SESSION_DETAIL)
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
                + tests + (f" Edited files: {j.output_dir}/" if j.output_dir else "")
            )
        held = sorted(w for r in reports for w in r.queue_waits_ms)
        if held:
            lines.append(f"BharatCode held these requests in its queue for {held[len(held) // 2] / 1000:.1f} s "
                         f"(median), {held[-1] / 1000:.1f} s at most.")
        lines.append(SESSION_DETAIL)
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
        text = (f"Cancelled job {job.id} ({job.kind} {job.target}). Nothing was copied to "
                f"{worktree.OUTPUT_DIR}/; anything it had finished stays on branch {worktree.branch_name(job.id)}.")
        return text + (f"\nNote: {outcome.note}" if outcome.note else "")

    def waits(self) -> str:
        path = db_path(self._root())
        with Store(path if path.exists() else ":memory:") as store, self._http() as http:  # needs no jobs
            return waits.describe(waits.views(store, http))

    def ask(self, question: str, your_model: str | None = None) -> str:
        """Answer a general question on whichever model starts it sooner, and say which and why.
        Runs as a read-only Desktop session in spare-gpu's own folder, never in the user's repo
        (a session writes its permission file into its folder)."""
        question = (question or "").strip()
        if not question:
            return "Nothing to ask: give the question."
        try:
            conn = desktop.discover()
        except desktop.DesktopNotFound as exc:
            return f"Didn't ask: {exc}"
        path = db_path(self._root())
        folder = path.parent if path.exists() else None
        with Store(path if path.exists() else ":memory:") as store, self._http() as http:
            state = router.State.load(folder) if folder else router.State()
            # Unlike job types, general questions have no measured quality check: the user asked for
            # them to go to the faster model, so they're treated as switch-safe here.
            choice = router.choose(QUESTION, self._preferred(your_model), waits.views(store, http), state,
                                   {"types": {QUESTION: "switch-safe"}})
            if folder:
                state.save(folder)
        workdir = ask_folder()
        prompt = f"{question}\n\nAnswer directly and briefly. Don't use any tools."
        result = desktop.DesktopAgent(conn, model=f"bharatcode/{choice.model}", timeout=ASK_TIMEOUT).run(
            workdir, prompt, edit=False, commands=())
        how = f"Answered by {waits.name(choice.model)}: {choice.reason}."
        if result.error or not result.text:
            refused = router.plan_refusal(result.error)
            if refused and refused != choice.model:  # the plan doesn't include it: ask the one it does
                result = desktop.DesktopAgent(conn, model=f"bharatcode/{refused}", timeout=ASK_TIMEOUT).run(
                    workdir, prompt, edit=False, commands=())
                how = f"Answered by {waits.name(refused)}: your plan doesn't include {waits.name(choice.model)}."
            if result.error or not result.text:
                return f"Couldn't get an answer: {result.error or 'the model returned nothing'}. ({how})"
        return f"{result.text}\n\n({how})"

    def _preferred(self, your_model: str | None) -> str:
        """The model the task would otherwise run on: the calling chat's own model if it said;
        else Desktop's default model; else DeepSeek. GTU_PREFERRED_MODEL overrides all of these."""
        if your_model:
            return router.preferred_model(your_model)
        try:
            default = desktop.default_model(desktop.discover())
        except desktop.DesktopNotFound:
            default = None
        return router.preferred_model(default or router.DELEGATE_PREFERRED)

    def delegate(self, kind: str, path: str, note: str | None = None, your_model: str | None = None) -> str:
        """Queue a task on whichever model starts it sooner (if its type is switch-safe), pinned to
        that model, and start the runner. Returns a short summary, never the work itself."""
        try:
            outcome = actions.queue_job(lambda repo: Store(db_path(repo)), self._root(), kind, path, note)
        except actions.ActionError as exc:
            return f"Didn't hand it over: {exc}"
        job = outcome.job
        with Store(db_path(Path(job.repo))) as store:
            preferred = self._preferred(your_model)
            choice = router.decide(store, store.path.parent, job.kind, preferred, self._http)
            store.set_route(job.id, choice.model, choice.preferred, choice.reason)
            runner_line = self._ensure_runner(store, Path(job.repo))
        lines = [f"Handed off: {job.kind} for {job.target} (job {job.id}), working in the background. When it "
                 f"finishes, its edited files are copied to {worktree.OUTPUT_DIR}/<date>_<time>/ in this project; "
                 "your files aren't touched.", choice.summary()]
        if outcome.note:
            lines.append(outcome.note)
        lines += [runner_line, "spare_work_status shows progress; spare_last_night shows what it produced."]
        return "\n".join(lines)

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
        in the background, unattended; when it finishes, its edited files are copied to the
        project's spare-gpu/<date>_<time>/ folder. It doesn't happen in this chat.

        Use it when the user wants tests, docs or a review done in the background, later, tonight
        or overnight. Examples: "queue tests for the payments module", "write tests for
        src/payments tonight", "get docs written for utils overnight", "queue a code review of
        app/api.py", "review app/api.py later". Call it straight away: it is the whole job, and it
        checks the path itself.
        To have it done now on whichever model is free instead, use spare_delegate. For any other
        command (a build, a migration, a big refactor script) use spare_run_when_free.

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
        for how long each model would wait right now use spare_waits."""
        return tools.work_status()

    @server.tool(annotations=notes(True))
    def spare_last_night(hours: float | None = None) -> str:
        """What spare-gpu got done recently: which jobs ran, when, how long, what each produced
        (the edited files in its spare-gpu/<date>_<time>/ folder, tests kept or discarded) and what it cost in tokens.

        Use it for a summary of results: "what did spare-gpu do last night?", "what got done?",
        "show me the overnight results". For one job's state ("is my tests job done?") or work still
        queued or running, use spare_work_status. Also answers "what did last night cost?": it gives
        the token totals and the budget used.

        Args:
            hours: How far back to look. Leave it out for the last 24 hours ("last night", "today");
                168 for "this week".
        """
        return tools.last_night(hours)

    @server.tool(annotations=notes(False, destructive=True))
    def spare_cancel_work(job_id: str) -> str:
        """Cancel a queued or running spare-gpu job, or a command waiting in spare_run_when_free.
        Nothing of a cancelled job is copied to the spare-gpu/ folder.

        Use it for "cancel the docs job", "drop job 1a2b3c4d", "don't run that command after all",
        "stop the tests for payments".

        Args:
            job_id: The 8-character id of the job or command. If the user describes it instead,
                call spare_work_status first to find the id.
        """
        return tools.cancel_work(job_id)

    @server.tool(annotations=notes(True, world=True))
    def spare_waits() -> str:
        """How long work would wait on each BharatCode model right now (DeepSeek and Qwen), from
        measured figures: BharatCode's own wait percentiles for the last hour, whether each lane has
        a free slot, the user's own recent waits, and what's usual at this hour; then which model
        would start sooner. Says so when there isn't enough data.

        Use it for timing questions: "is now a good time to run a big refactor?", "is BharatCode
        busy right now?", "how long is DeepSeek's queue?", "which model is faster right now?". It
        only reports; to hand a task to the faster model use spare_delegate. For why an
        already-queued job hasn't started, use spare_work_status."""
        return tools.waits()

    @server.tool(annotations=notes(True, world=True))
    def spare_ask(question: str, your_model: str | None = None) -> str:
        """Answer a general question (not a coding chore) on whichever BharatCode model will start
        it sooner, so the user doesn't sit in DeepSeek's queue for a quick answer. Waits for the
        answer and returns it, with which model answered and why. The answering model has no tools
        and no web access, so it answers from what it knows.

        Use it when the user asks a question through /auto, or says "ask whichever model is free:
        ...". Pass the answer on as it comes back. For tests, docs or a code review of files in the
        repo use spare_delegate instead.

        Args:
            question: The user's question, in their words.
            your_model: The model the question would otherwise go to, e.g. "deepseek-v4.1-flash".
        """
        return tools.ask(question, your_model)

    @server.tool(annotations=notes(False, world=True))
    def spare_delegate(type: Literal["tests", "docs", "review"], path: str, note: str | None = None,
                       your_model: str | None = None) -> str:
        """Hand a self-contained task (write tests, write docs, or review code for a file or folder
        in this repo) to a background BharatCode Desktop session on whichever model will start it
        sooner, so the user doesn't wait in DeepSeek's queue. Only task types measured as
        switch-safe ever move to the other model; the result is checked before it's kept, and if
        the check keeps nothing it reruns once on the preferred model. Returns at once with a short
        summary; when it finishes, its edited files are in spare-gpu/<date>_<time>/ in the project.
        The work itself never comes back into this chat.

        Use it when the user wants the task done now without waiting: "write tests for src/pay.py
        on whichever model is free", "get this reviewed now, don't wait in the queue", "delegate the
        docs for utils". For work that can simply wait in the background (later, tonight), use
        spare_queue_work.

        Args:
            type: "tests" writes new passing tests; "docs" adds docstrings and README text without
                changing code; "review" writes a code review.
            path: File or folder relative to the repo root, e.g. "src/pay.py". Pass the user's path
                exactly as given; spare-gpu says if it doesn't exist.
            note: Optional extra instructions in the user's words.
            your_model: The model you are running on, as your system prompt names it (e.g.
                "deepseek-v4.1-flash"); it's the one the task would otherwise use.
        """
        return tools.delegate(type, path, note, your_model)

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
