"""MCP server: spare-gpu as tools an agent can call (BharatCode Desktop, or any MCP client).

Started with `gtu mcp`, over stdio. Five tools, each shaped around a task the user wants done
rather than around our internal functions. Selection accuracy drops as tool count grows and when
tools overlap, so related questions are merged (one status tool answers "what's queued", "is it
done" and "where are its files"). Each tool is a thin wrapper over the code the matching `gtu`
command uses, sharing the repo's .gtu/gtu.db (safe alongside a `gtu start` in a terminal).
Tools return plain text for the agent to relay. spare_delegate runs the job in front of the user,
as a visible Desktop session, and returns when it is done; while it runs, the server sends MCP
progress notifications so the client's per-call timeout keeps being reset.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import httpx
from mcp.server.mcpserver import Context  # at module level: the tool's annotation must resolve

from . import __version__, actions, desktop, pack, refs, router, runstate, scholar, security, stats, waits, workers, worktree
from .api import ApiKeyMissing
from .budget import Budgets
from .jobs import PLANS, JobConfig
from .scheduler import Scheduler
from .store import DB_ENV, FINAL_STATUSES, Store, StoreError

INSTRUCTIONS = (
    "spare-gpu does coding chores (writing tests, adding docs, code review, applying a fix) for a project "
    "folder as BharatCode Desktop sessions the user can watch, in a private copy of the project, never "
    "touching the user's checkout: each finished job's edited files are copied to the project's "
    "spare-gpu/<date>_<time>/ folder. spare_delegate runs the job now and returns when it is done. It "
    "measures how long each model would wait right now, and can hand a switch-safe task to whichever "
    "model starts it sooner, checking the result before keeping it. Relay the tools' answers to the user "
    "as they are."
)
# Desktop already shows each session's tokens in detail; spare-gpu points there rather than estimate.
SESSION_DETAIL = ("Each job runs as a BharatCode Desktop session named \"spare-gpu: ...\"; open it in Desktop "
                  "for its full token breakdown.")


class NoStore(Exception):
    """There's no .gtu store for this project yet."""


def project_root(base: Path) -> Path:
    """The git repo containing `base`, or `base` itself outside a repo."""
    return worktree.project_root(base)


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

    def __init__(self, base: Path, http: Callable[[], httpx.Client] | None = None,
                 scholar_http: Callable[[], httpx.Client] | None = None,
                 make_worker: Callable[..., workers.Worker] | None = None, plans: dict | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = Path(base)
        # Everything that reaches outside is injectable, so tests use fakes: the worker that does the
        # steps (Desktop or the API), the job plans, the wait figures (BharatCode's live stats), and
        # the scholarly registries.
        self._http = http or (lambda: httpx.Client(timeout=10.0))
        self._scholar_http = scholar_http or scholar.client
        self._make_worker = make_worker or workers.build
        self._plans = plans or PLANS
        self._sleep = sleep
        self._running = threading.Lock()  # one job at a time in this process; a second call waits its turn

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

    # --- the five tools -------------------------------------------------------------------

    def work_status(self) -> str:
        try:
            with self._open() as store:
                jobs = store.list_jobs()
                tokens = {j.id: store.request_summary(j.id)["total_tokens"] for j in jobs}
                pid = runstate.running_pid(store.path.parent)
                waiting = [j for j in jobs if j.status in ("queued", "paused")]
        except NoStore as exc:
            return str(exc)
        if not jobs:
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
        if pid:
            lines.append(f"A job is running now (process {pid}); it shows in Desktop as a session named "
                         "\"spare-gpu: ...\".")
        elif waiting:
            lines.append("Nothing is running now. Waiting jobs start with `gtu start` in a terminal; a job handed "
                         "over with spare_delegate runs at once.")
        if jobs:
            lines.append(SESSION_DETAIL)
        return "\n".join(lines)

    def cancel_work(self, work_id: str) -> str:
        work_id = work_id.strip()
        try:
            with self._open() as store:
                outcome = actions.cancel_job(store, work_id)
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
            text += "\n" + self._role_lines(views, self._root() / ".gtu")
            self._snapshot_usage()
        return text

    def _role_lines(self, views, folder: Path) -> str:
        """For each switchable pack role: which variant to start now. The router decides as it does
        for jobs: down to the standard model only when the role is measured switch-safe (in
        routing_policy.json, or by hand in the project's .gtu/switch-safe or GTU_SWITCH_SAFE) and
        that saves enough waiting; up is always allowed. Each role is judged from the same saved
        state (which models the plan refused), on a copy: an answer is a reading, not a routing,
        so the jobs' hysteresis is neither consumed by sibling roles nor changed here."""
        heavy = router.preferred_model(router.DELEGATE_PREFERRED)
        policy = router.load_policy(folder)
        saved = router.State.load(folder)
        lines = ["Pack roles, which variant to start now (routing_policy.json and the waits above decide):"]
        for role in pack.SWITCHABLE:
            state = router.State(saved.away_from, saved.back_votes, list(saved.unavailable))
            choice = router.choose(role, heavy, views, state, policy)
            agent = pack.variant_of(role) if choice.routed else role
            lines.append(f"- {role}: start `{agent}` ({choice.reason})")
        return "\n".join(lines)

    # --- the pack's cost figures: the time of spare_waits, then Desktop's own counts since ----------

    def _usage_file(self) -> Path:
        return self._root() / ".gtu" / "pack-usage.json"

    def _snapshot_usage(self) -> None:
        try:
            folder = self._usage_file().parent
            folder.mkdir(parents=True, exist_ok=True)
            marker = folder / ".gitignore"  # spare-gpu's own folder stays out of the user's repo, as the store keeps it
            if not marker.exists():
                marker.write_text("*\n", encoding="utf-8")
            self._usage_file().write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                                          encoding="utf-8")
        except OSError:
            pass

    def pack_cost(self) -> str:
        try:
            started = datetime.fromisoformat(json.loads(self._usage_file().read_text(encoding="utf-8"))["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return ("No start time: call spare_waits at the start of the command, then pack_cost at the end. "
                    "Say the token figures weren't available.")
        figures = stats.tokens_since(started, self._root())
        if figures is None:
            return ("BharatCode Desktop's session data wasn't found on this machine, so there are no token figures. "
                    "Say so instead of estimating.")
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        lines = [f"Tokens used in this project's BharatCode Desktop sessions since the spare_waits call "
                 f"{minutes:.0f} min ago, by model (exact, from Desktop's own record of every reply):"]
        for model, used in sorted(figures.items()):
            lines.append(f"- {waits.name(model)}: {used['tokens']:,} tokens in {used['messages']} message(s) "
                         f"({used['input']:,} input, {used['output'] + used['reasoning']:,} output, "
                         f"{used['cache_read']:,} read from cache)")
        if len(lines) == 1:
            lines.append("- no replies recorded yet.")
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
        folder = self._root() / ".gtu"
        if your_model:
            return router.preferred_model(your_model, folder)
        try:
            default = desktop.default_model(desktop.discover())
        except desktop.DesktopNotFound:
            default = None
        return router.preferred_model(default or router.DELEGATE_PREFERRED, folder)

    def delegate(self, kind: str, path: str, note: str | None = None, your_model: str | None = None,
                 progress: Callable[[str], None] = lambda line: None) -> str:
        """Queue a task on whichever model starts it sooner (if its type is switch-safe), pinned to
        that model, run it now, in front of the user, and return how it went. `progress` gets each
        line the runner would print (the MCP layer turns them into progress notifications)."""
        try:
            outcome = actions.queue_job(lambda repo: Store(db_path(repo)), self._root(), kind, path, note)
        except actions.ActionError as exc:
            return f"Didn't hand it over: {exc}"
        job = outcome.job
        with Store(db_path(Path(job.repo))) as store:
            preferred = self._preferred(your_model)
            choice = router.decide(store, store.path.parent, job.kind, preferred, self._http)
            store.set_route(job.id, choice.model, choice.preferred, choice.reason)
            result = self._run_now(store, job.id, progress)
        lines = [result, choice.summary()]
        if outcome.note:
            lines.append(outcome.note)
        return "\n".join(lines)

    def _run_now(self, store: Store, job_id: str, progress: Callable[[str], None]) -> str:
        """Run one queued job to its end and say how it went. Another run in this process goes
        first; a `gtu start` (or another server) already running in the folder keeps the job queued
        for its loop, so two runners never work the same queue."""
        folder = store.path.parent
        job = store.get_job(job_id)
        what = f"{job.kind} for {job.target} (job {job.id})"
        with self._running:
            other = runstate.running_pid(folder)
            if other and other != os.getpid():
                return (f"Queued: {what} waits behind the run already going in this project (process {other}), "
                        "which picks it up next. spare_work_status shows it.")
            try:
                job_config = JobConfig.load(Path(job.repo))
                budgets = Budgets.load(Path(job.repo))
            except ValueError as exc:
                return f"Queued, not started: {exc}"
            try:
                worker = self._make_worker(store, progress, job_config)
            except ApiKeyMissing as exc:
                return (f"Queued, not started: {what} needs BharatCode Desktop open, or an API key ({exc}). "
                        "Run `gtu setup` in a terminal, then `gtu start`.")
            runstate.clear(folder)
            runstate.write_run(folder)
            try:
                scheduler = Scheduler(
                    store, worker.client, agent=worker.agent, plans=self._plans, budgets=budgets,
                    config=job_config, log=progress, sleep=self._sleep, desktop_missing=worker.desktop_missing,
                    should_stop=lambda: runstate.stop_requested(folder) or store.get_job(job_id).status == "cancelled",
                )
                status = scheduler.run_job(job_id)
            finally:
                runstate.clear(folder)
                worker.close()
        return self._outcome(store, store.get_job(job_id), status, worker.inside_desktop)

    def _outcome(self, store: Store, job, status: str, inside_desktop: bool) -> str:
        what = f"{job.kind} for {job.target} (job {job.id})"
        tokens = store.request_summary(job.id)["total_tokens"]
        spent = f" It used {tokens:,} tokens." if tokens else ""
        where = (" Its full transcript is the Desktop session named \"spare-gpu: ...\"." if inside_desktop else "")
        if status == "done":
            if job.output_dir:
                return (f"Done: {what} finished; its edited files are in {job.output_dir}/ in this project. Your "
                        f"files weren't touched.{spent}{where}")
            return f"Done: {what} finished, but it changed no files.{spent}{where}"
        if status == "failed":
            step = next((s for s in store.steps(job.id) if s["status"] == "failed"), None)
            why = f": {step['output']}" if step and step["output"] else ""
            return f"Failed: {what}{why}.{spent}{where}".replace("..", ".")
        if status == "cancelled":
            return f"Cancelled: {what} was stopped; nothing was copied to {worktree.OUTPUT_DIR}/.{spent}"
        if status == "waiting":
            return (f"Queued, not started: {what} needs BharatCode Desktop. {desktop.DESKTOP_NEEDED}")
        try:
            used = Budgets.load(Path(job.repo)).per_job
        except ValueError:
            used = Budgets().per_job
        return (f"Paused: {what} stopped before it finished"
                + (f" (its token budget of {used:,} is used up; raise GTU_JOB_TOKEN_BUDGET)" if tokens >= used else
                   " (a stop was asked for)")
                + f". Progress is saved; `gtu start` in a terminal resumes it.{spent}")


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

    def build_report(self, folder: str, title: str = "") -> str:
        root = self._root()
        path = Path(folder.strip() or ".")
        path = (path if path.is_absolute() else root / path).resolve()
        if root not in path.parents and path != root:
            return f"Didn't build it: {folder} is outside this project ({root})."
        try:
            _, summary = refs.build_report(path, title)
        except FileNotFoundError:
            return (f"No section files in {path / refs.SECTIONS_DIR}. Each writer saves its section there as "
                    "<number>-<name>.md first.")
        except (OSError, ValueError) as exc:
            return f"Couldn't build the report: {exc}."
        return summary

    def security_filter(self, findings: str) -> str:
        try:
            candidates = security.parse(findings)
        except ValueError as exc:
            return f"Couldn't read the candidates: {exc}."
        return security.describe(security.filter_findings(candidates))


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
        running, and what finished (with job ids, tokens and where each job's files landed).

        Use it for any "where are things" question: "what's queued?", "is my tests job done?",
        "what is spare-gpu doing?", "why hasn't my job started?", "what did spare-gpu do
        today?". Also use it to find a job id before cancelling. For how long each model would
        wait right now use spare_waits; the full summary with token totals is `gtu report` in a
        terminal."""
        return tools.work_status()

    @server.tool(annotations=notes(False, destructive=True))
    def spare_cancel_work(job_id: str) -> str:
        """Cancel a queued or running spare-gpu job. Nothing of a cancelled job is copied to the
        spare-gpu/ folder.

        Use it for "cancel the docs job", "drop job 1a2b3c4d", "stop the tests for payments".

        Args:
            job_id: The 8-character id of the job. If the user describes it instead, call
                spare_work_status first to find the id.
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

        @server.tool(annotations=notes(False))
        def build_report(folder: str, title: str = "") -> str:
            """Put /deep-research's report together: joins <folder>/sections/*.md in number order into
            <folder>/report.md under the title, then writes its References section from sources.json
            (every cited key, leaving out what verify_refs marked DROP). Overwrites report.md, so
            edits go into the section files and the report is built again. Names any cited key that
            sources.json lacks or dropped.

            Args:
                folder: The research folder (e.g. research/transformers), relative to the project.
                title: The report's title line.
            """
            return tools.build_report(folder, title)

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
        def pack_cost() -> str:
            """The tokens each model used in this project's Desktop sessions since the spare_waits
            call that started the pack command, exact, from Desktop's own record of every reply: for
            the cost summary at the end of /deep-research, /code-review, /security-review and
            /simplify. When it has no figures, say so; never estimate them."""
            return tools.pack_cost()

    @server.tool(annotations=notes(False, world=True))
    async def spare_delegate(type: Literal["tests", "docs", "review", "fix"], path: str, note: str | None = None,
                             your_model: str | None = None, ctx: Context | None = None) -> str:
        """Run a self-contained task (write tests, write docs, review code, or apply a fix for a file
        or folder in this repo) now, as a BharatCode Desktop session the user can watch, on
        whichever model will start it sooner, so the user doesn't wait in DeepSeek's queue. Only task
        types measured as switch-safe ever move to the other model; the result is checked before
        it's kept, and if the check keeps nothing it reruns once on the preferred model. The call
        returns when the job is done (minutes for tests and docs), with the result and where its
        edited files are: spare-gpu/<date>_<time>/ in the project. The user's own files are never
        touched, and the work itself never comes back into this chat.

        Use it when the user wants the task done now: "write tests for src/pay.py on whichever
        model is free", "get this reviewed now, don't wait in the queue", "delegate the docs for
        utils", "run tests for the payments module". For a plain question (not a coding chore) use
        spare_ask. Work for later is queued from a terminal with `gtu add` and run by `gtu start`.

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
        return await run_with_progress(ctx, lambda progress: tools.delegate(type, path, note, your_model, progress))

    return server


HEARTBEAT_S = 10.0  # progress notifications this often keep the client's per-call timeout being reset


async def run_with_progress(ctx, work: Callable[[Callable[[str], None]], str]) -> str:
    """Run `work` (blocking) in a thread, and while it runs send the client a progress notification
    every few seconds carrying the latest line `work` reported. BharatCode Desktop resets its tool
    timeout on each one, so a job that takes minutes is fine; a client that asked for no progress
    gets none (the SDK makes report_progress a no-op then)."""
    import anyio

    lines: list[str] = []
    done = anyio.Event()

    async def heartbeat() -> None:
        sent = 0
        while not done.is_set():
            with anyio.move_on_after(HEARTBEAT_S):
                await done.wait()
            if ctx is not None and (lines or not done.is_set()):
                sent += 1
                try:
                    await ctx.report_progress(sent, None, lines[-1] if lines else "working")
                except Exception:  # noqa: BLE001  progress is best effort; the result still comes back
                    pass

    async def run() -> str:
        try:
            return await anyio.to_thread.run_sync(work, lines.append)
        finally:
            done.set()

    async with anyio.create_task_group() as group:
        group.start_soon(heartbeat)
        result = await run()
    return result


def serve(base: Path, pack: bool = False) -> None:
    build_server(base, pack=pack).run("stdio")
