"""MCP server: spare-gpu as tools an agent can call (BharatCode's CLI, or any MCP client).

Started with `gtu mcp`, over stdio. Five tools, each shaped around a task the user wants done
rather than around our internal functions. Selection accuracy drops as tool count grows and when
tools overlap, so related questions are merged (one status tool answers "what's queued", "is it
done" and "why isn't it running"). Each tool is a thin wrapper over the code the matching `gtu`
command uses, sharing the repo's .gtu/gtu.db (safe alongside a running `gtu start`). Tools return
plain text for the agent to relay, and none blocks: long work is handed to a background process.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import httpx

from . import __version__, actions, desktop, freerun, overhead, pack, refs, router, runner, runstate, scholar, security, waits, worktree
from .api import ApiKeyMissing, find_api_key
from .gate import Gate, GateConfig
from .store import DB_ENV, FINAL_STATUSES, Store, StoreError

INSTRUCTIONS = (
    "spare-gpu does coding chores (writing tests, adding docs, code review) for a project folder as "
    "background BharatCode Desktop sessions, never touching the user's checkout: each finished job's "
    "edited files are copied to the project's spare-gpu/<date>_<time>/ folder. It measures how long each model would wait right now, and can hand a "
    "switch-safe task to whichever model starts it sooner, checking the result before keeping it. "
    "Relay the tools' answers to the user as they are."
)
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
    """The five tools as plain functions returning text, for one project folder."""

    def __init__(self, base: Path, make_gate: Callable[[Store], Gate] | None = None,
                 launch: Callable[[Path], int] | None = None,
                 http: Callable[[], httpx.Client] | None = None,
                 scholar_http: Callable[[], httpx.Client] | None = None):
        self.base = Path(base)
        # Everything that reaches outside is injectable, so tests use fakes: the gate (BharatCode's
        # live stats), the background job runner, the wait figures, and the scholarly registries.
        self._make_gate = make_gate or (lambda store: Gate(store, GateConfig.from_env()))
        self._launch = launch or runner.launch
        self._http = http or (lambda: httpx.Client(timeout=10.0))
        self._scholar_http = scholar_http or scholar.client

    # --- helpers -------------------------------------------------------------------------

    def _root(self) -> Path:
        return project_root(self.base)

    def _open(self, create: bool = False) -> Store:
        path = db_path(self._root())
        if not create and not path.exists():
            raise NoStore(
                f"spare-gpu hasn't been used in this project yet (no .gtu folder in {self._root()}). "
                "Hand something over with spare_delegate first, or queue it with `gtu add` in a terminal."
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

    # --- the five tools -------------------------------------------------------------------

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
                f"{worktree.OUTPUT_DIR}/.")
        return text + (f"\nNote: {outcome.note}" if outcome.note else "")

    def waits(self, roles: bool = False) -> str:
        path = db_path(self._root())
        with Store(path if path.exists() else ":memory:") as store, self._http() as http:  # needs no jobs
            views = waits.views(store, http)
        text = waits.describe(views)
        if roles:
            text += "\n" + self._role_lines(views, path.parent if path.exists() else None)
            self._snapshot_usage()
        return text

    def _role_lines(self, views, folder: Path | None) -> str:
        """For each switchable pack role: which variant to start now. The router decides as it does
        for jobs: down to the standard model only when the role is measured switch-safe and that
        saves enough waiting; up is always allowed."""
        heavy = router.preferred_model(router.DELEGATE_PREFERRED)
        state = router.State.load(folder) if folder else router.State()
        lines = ["Pack roles, which variant to start now (routing_policy.json and the waits above decide):"]
        for role in pack.SWITCHABLE:
            choice = router.choose(role, heavy, views, state)
            agent = pack.variant_of(role) if choice.routed else role
            lines.append(f"- {role}: start `{agent}` ({choice.reason})")
        if folder:
            state.save(folder)
        return "\n".join(lines)

    # --- the pack's cost figures: a usage snapshot at spare_waits, the delta at pack_cost -----------

    def _usage_file(self) -> Path:
        return self._root() / ".gtu" / "pack-usage.json"

    def _snapshot_usage(self) -> None:
        figures = overhead.model_usage(days=1)
        if figures is None:
            return
        try:
            self._usage_file().parent.mkdir(parents=True, exist_ok=True)
            self._usage_file().write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                      "models": figures}), encoding="utf-8")
        except OSError:
            pass

    def pack_cost(self) -> str:
        try:
            snapshot = json.loads(self._usage_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ("No usage snapshot: call spare_waits at the start of the command, then pack_cost at the end. "
                    "Say the token figures weren't available.")
        now_figures = overhead.model_usage(days=1)
        if now_figures is None:
            return ("BharatCode's CLI isn't available here, so there are no token figures; Desktop's session "
                    "detail has them. Say so instead of estimating.")
        started = datetime.fromisoformat(snapshot["at"])
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        lines = [f"Tokens used on this machine since the spare_waits call {minutes:.0f} min ago, by model "
                 "(from `bharatcode stats --models`, rounded by BharatCode to about a hundred tokens; every "
                 "session on this machine in that time counts):"]
        before = snapshot.get("models") or {}
        for model, figures in now_figures.items():
            earlier = before.get(model) or {}
            delta = figures["tokens"] - earlier.get("tokens", 0.0)
            messages = figures["messages"] - earlier.get("messages", 0.0)
            if delta <= 0 and messages <= 0:
                continue
            lines.append(f"- {waits.name(model)}: +{delta:,.0f} tokens in {messages:.0f} message(s)")
        if len(lines) == 1:
            lines.append("- nothing new was recorded yet (BharatCode writes its stats when a session ends).")
        return "\n".join(lines)

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
        lines += [runner_line, "spare_work_status shows progress and, once it finishes, where its files are."]
        return "\n".join(lines)


    # --- the pack's tools (only with `gtu mcp --pack`: every tool costs every session tokens) ---

    def scholar_search(self, query: str, limit: int = 5, sources: str | None = None) -> str:
        chosen = tuple(s.strip().lower() for s in (sources or "").split(",") if s.strip()) or scholar.SOURCES
        try:
            with self._scholar_http() as http:
                papers, notes = scholar.search(query, limit=limit, sources=chosen, http=http)
        except scholar.ScholarError as exc:
            return f"Couldn't search: {exc}."
        return scholar.describe(papers, notes)

    def verify_refs(self, folder: str) -> str:
        root = self._root()
        path = Path(folder.strip() or ".")
        path = (path if path.is_absolute() else root / path).resolve()
        if root not in path.parents and path != root:
            return f"Didn't check it: {folder} is outside this project ({root})."
        try:
            with self._scholar_http() as http:
                verdicts, file = refs.verify_file(path, http=http)
        except FileNotFoundError:
            return (f"No {refs.SOURCES_FILE} in {path}. The scouts' ids go there first, as "
                    '{"sources": [{"key": ..., "title": ..., "authors": [...], "year": ..., "doi"/"arxiv"/"openalex"/"url": ...}]}.')
        except (OSError, ValueError) as exc:
            return f"Couldn't read {refs.SOURCES_FILE}: {exc}."
        return refs.describe(verdicts, file)

    def security_filter(self, findings: str) -> str:
        try:
            candidates = security.parse(findings)
        except ValueError as exc:
            return f"Couldn't read the candidates: {exc}."
        return security.describe(security.filter_findings(candidates))

    def context_report(self, days: int = 7, measure: bool = False) -> str:
        report = overhead.inventory(self._root())
        if measure:
            report.measured = overhead.measure(self._root())
            if report.measured is None:
                report.notes.append("A live measurement wasn't possible: BharatCode's CLI isn't available here.")
        report.usage = overhead.usage(days)
        return overhead.describe(report)


def build_server(base: Path, tools: SpareTools | None = None, pack: bool = False):
    """The MCP server for one project folder (the agent's working directory by default). With
    `pack`, the command pack's tools too: they exist only for a server started by a pack install,
    and the pack's config hides them from every agent but the pack's own."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tools = tools or SpareTools(base)
    server = MCPServer("spare-gpu", version=__version__, instructions=INSTRUCTIONS)

    def notes(read_only: bool, destructive: bool = False, world: bool = False) -> ToolAnnotations:
        return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive, open_world_hint=world)

    @server.tool(annotations=notes(True, world=True))
    def spare_work_status() -> str:
        """Everything about spare-gpu's work in this repo in one answer: what's queued, what's
        running, what finished (with job ids and tokens), and, if something is waiting, why it
        isn't running yet (BharatCode's current load).

        Use it for any "where are things" question: "what's queued?", "is my tests job done?",
        "what is spare-gpu doing?", "why hasn't my job started?", "did my command run?", "what did
        spare-gpu do last night?". Also use it to find a job id before cancelling. For how long
        each model would wait right now use spare_waits; the full morning summary with token
        totals is `gtu report` in a terminal."""
        return tools.work_status()

    @server.tool(annotations=notes(False, destructive=True))
    def spare_cancel_work(job_id: str) -> str:
        """Cancel a queued or running spare-gpu job, or a command queued with `gtu run-when-free`.
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
        return tools.waits(roles=pack)

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

    if pack:
        @server.tool(annotations=notes(True, world=True))
        def scholar_search(query: str, limit: int = 5, sources: str | None = None) -> str:
            """Search the open scholarly registries (OpenAlex, Crossref, arXiv) for papers on a topic:
            for /deep-research's scouts. Returns up to `limit` papers from each registry, merged, each
            with title, authors, year, venue, exact ids (DOI, arXiv, OpenAlex), URL, a short abstract
            and citation count. Cite a paper only with an id from here or from a page you fetched,
            never from memory. Google Scholar isn't used: it forbids automated access.

            Args:
                query: Topic words or a title, in plain words (no boolean syntax).
                limit: Papers per registry, 1 to 25 (default 5).
                sources: Comma-separated subset of openalex, crossref, arxiv (default: all three).
            """
            return tools.scholar_search(query, limit, sources)

        @server.tool(annotations=notes(False))
        def verify_refs(folder: str) -> str:
            """Check every reference in <folder>/sources.json against the registries (DOI, arXiv id,
            OpenAlex id, or a URL that must answer and carry the title), for /deep-research's checker
            before a report is final. A reference is kept only when its id resolves and the registry's
            title, authors and year match the report's; the verdicts are written back into the file,
            and the answer lists what to keep and what to DROP.

            Args:
                folder: The research folder holding sources.json (e.g. research/transformers), relative to
                    the project, or the file itself.
            """
            return tools.verify_refs(folder)

        @server.tool(annotations=notes(True))
        def security_filter(findings: str) -> str:
            """Drop the candidate findings of /security-review that match known false-positive
            patterns, by rule and before any model judges them: denial of service, rate limiting,
            resource leaks, open redirects, regex injection, memory safety outside C/C++, SSRF in
            HTML, documentation and test-only files, attacks that need an environment variable,
            and candidates below 0.7 confidence. Returns what was dropped and why, and the kept
            candidates as JSON for the verifier.

            Args:
                findings: The candidates as JSON: a list of objects with file, line, severity, category,
                    description, exploit_scenario, recommendation and confidence (0 to 1).
            """
            return tools.security_filter(findings)

        @server.tool(annotations=notes(True))
        def context_report(days: int = 7, measure: bool = False) -> str:
            """What every BharatCode request in this project carries before the user's words, item by
            item with its token cost (measured per-item costs, counted from the config and files):
            the system prompt and built-in tools, each MCP server, the skills found, instruction
            files like AGENTS.md, and the command pack. Then the top savings, each with the exact
            config switch and the `gtu context-report --apply N` command that makes it after a
            backup, and BharatCode's usage figures for the last days when its CLI is installed.
            For /context-report: relay the numbers; never invent any.

            Args:
                days: How many days of usage to ask BharatCode's stats for (default 7).
                measure: Also run one tiny `bharatcode run` to measure the real overhead (costs a model call; default off).
            """
            return tools.context_report(days, measure)

        @server.tool(annotations=notes(True))
        def pack_cost() -> str:
            """The tokens each model used on this machine since the spare_waits call that started the
            pack command, from `bharatcode stats --models`: for the cost summary at the end of
            /deep-research, /code-review, /security-review and /simplify. Rounded by BharatCode to
            about a hundred tokens, and counting every session on this machine in that time. When it
            has no figures, say so; never estimate them."""
            return tools.pack_cost()

    @server.tool(annotations=notes(False, world=True))
    def spare_delegate(type: Literal["tests", "docs", "review", "fix"], path: str, note: str | None = None,
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
        docs for utils", "queue tests for the payments module". For a plain question (not a
        coding chore) use spare_ask. Work that should wait for a quiet moment is queued from a
        terminal with `gtu add` and run by `gtu start`.

        Args:
            type: "tests" writes new passing tests; "docs" adds docstrings and README text without
                changing code; "review" writes a code review; "fix" applies the review findings given
                in `note` (from /code-review --fix) and keeps the result only if the project's test
                command passes with it.
            path: File or folder relative to the repo root, e.g. "src/pay.py" ("." for a fix). Pass the
                user's path exactly as given; spare-gpu says if it doesn't exist.
            note: Optional extra instructions in the user's words; for "fix", the findings, one per
                line with file:line and the change wanted.
            your_model: The model you are running on, as your system prompt names it (e.g.
                "deepseek-v4.1-flash"); it's the one the task would otherwise use.
        """
        return tools.delegate(type, path, note, your_model)

    return server


def serve(base: Path, pack: bool = False) -> None:
    build_server(base, pack=pack).run("stdio")
