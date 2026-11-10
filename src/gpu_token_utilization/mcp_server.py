"""MCP server: spare-gpu as tools an agent can call (BharatCode Desktop, or any MCP client).

BharatCode Desktop starts it (`gtu mcp`, over stdio). Five tools every chat sees, each shaped
around a task the user wants done rather than around our internal functions. Selection accuracy
drops as tool count grows and when tools overlap, so related questions are merged (one status tool
answers "what's queued", "is it done" and "where are its files"). With `--pack`, the pack's own
tools too, hidden from every agent but the pack's (each visible tool costs every step tokens):
among them the ones behind /apply, /undo, /cost, /prompts and /doctor. Every tool shares the repo's
.gtu/gtu.db and returns plain text for the agent to relay. spare_delegate runs the job in front of the user,
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

from . import __version__, actions, desktop, pack, refs, router, runstate, scholar, security, stats, tally, waits, workers, worktree
from .bc_config import SERVER_NAME
from .budget import Budgets
from .jobs import PLANS, JobConfig
from .scheduler import Scheduler
from .store import DB_ENV, FINAL_STATUSES, IGNORE, Store, StoreError

INSTRUCTIONS = (
    "spare-gpu runs a request on whichever BharatCode model starts it sooner (spare_auto), and applies "
    "review findings as a fix job (spare_delegate): a BharatCode Desktop session the user can watch, in "
    "a private copy of the project, never touching the user's checkout. Each finished job's edited files "
    "are copied to the project's spare-gpu/<date>_<time>/ folder; a fix is kept only if the project's "
    "tests pass with it. Relay the tools' answers to the user as they are."
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


QUESTION = "question"  # the router's "kind" for /auto's conversations
AUTO_TIMEOUT = 1800.0  # how long one /auto turn is waited on; after that it carries on in its session
AUTO_TOOL = "spare-gpu_spare_auto"  # the tool's name as Desktop shows it in a chat's messages
AUTO_SESSIONS = "auto-sessions.json"  # in .gtu: each chat's background session
LATEST = "latest"  # the key for the most recent one, used when the calling chat can't be found


def failed(result) -> bool:
    """Whether a turn failed because BharatCode couldn't serve the model (so the other may)."""
    if result.stopped == "unavailable":
        return True
    error = (result.error or "").lower()
    return "model unavailable" in error or "503" in error or "overloaded" in error


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
                "Hand something over with spare_delegate (or /auto) first."
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
            lines.append("Nothing is running now. A job that stopped before finishing (its token budget, or Desktop "
                         "closed) doesn't start again by itself: ask for the task again to run it afresh.")
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
                marker.write_text(IGNORE, encoding="utf-8")
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
        figures, runs = stats.usage_since(started, self._root(), caller_tool=f"{SERVER_NAME}_pack_cost")
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
        if runs is None:
            lines.append("Helper runs: not available (this Desktop doesn't record which session started which); "
                         "say the count wasn't available.")
        else:
            counted = ", ".join(f"{n} on {waits.name(model)}" for model, n in sorted(runs.items())) or "none"
            lines.append(f"Helper runs started since then (counted from Desktop's record, queued and failed ones "
                         f"included; report this number, don't count them yourself): {sum(runs.values())} ({counted}).")
        return "\n".join(lines)

    def auto(self, request: str, your_model: str | None = None, new: bool = False,
             progress: Callable[[str], None] = lambda line: None) -> str:
        """Do anything a normal BharatCode chat does, on whichever model starts it sooner: the
        request goes to a background Desktop session in the project, with Desktop's default agent
        and the user's own permissions. Each chat keeps its own session, so a follow-up remembers
        the earlier turns; the model is picked again for every turn. `new` starts a fresh one."""
        request = (request or "").strip()
        if not request:
            return "Nothing to do: give the request."
        try:
            conn = desktop.discover()
        except desktop.DesktopNotFound as exc:
            return f"Didn't start: {exc}"
        root = self._root()
        path = db_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with Store(path) as store, self._http() as http:  # Store also writes .gtu/.gitignore
            state = router.State.load(path.parent)
            # Like questions before: no measured quality check, and the user asked for the faster
            # model, so /auto's turns are treated as switch-safe.
            choice = router.choose(QUESTION, self._preferred(your_model), waits.views(store, http), state,
                                   {"types": {QUESTION: "switch-safe"}}, measured=router.own_start_times())
            state.save(path.parent)
        links_file = path.parent / AUTO_SESSIONS
        try:
            links = json.loads(links_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            links = {}
        chat = desktop.calling_session(conn, root, AUTO_TOOL)
        known = None if new else links.get(chat or LATEST)
        title = (known or {}).get("title") or f"auto: {request.splitlines()[0][:60]}"
        agent = desktop.DesktopAgent(conn, timeout=AUTO_TIMEOUT, poll=1.0)

        def turn(model: str):
            progress(f"Working on {waits.name(model)} in the '{title}' session.")
            return agent.converse(
                root, (known or {}).get("session"), request, title=title, model=f"bharatcode/{model}",
                on_step=lambda step: progress(f"Working on {waits.name(model)} in the '{title}' session."),
                on_approval=lambda: progress(f"Waiting for your approval: open the '{title}' session and approve it."))

        session, result = turn(choice.model)
        how = f"Answered by {waits.name(choice.model)}: {choice.reason}."
        refused = router.plan_refusal(result.error) if result.error else None
        other = next((m for m in waits.models() if m != choice.model and m not in state.unavailable), None)
        if refused and refused != choice.model:  # the plan doesn't include it: use the one it does
            known = {"session": session, "title": title} if session else known
            session, result = turn(refused)
            how = f"Answered by {waits.name(refused)}: your plan doesn't include {waits.name(choice.model)}."
        elif failed(result) and other:  # BharatCode says the model is unavailable: don't wait it out
            state.mark_failing(choice.model)
            state.save(path.parent)
            progress(f"{waits.name(choice.model)} is unavailable right now; switching to {waits.name(other)}.")
            known = {"session": session, "title": title} if session else known
            session, result = turn(other)
            how = f"Answered by {waits.name(other)}: {waits.name(choice.model)} was unavailable right now."
        if session:
            links[chat or LATEST] = links[LATEST] = {"session": session, "title": title}
            try:
                links_file.write_text(json.dumps(links, indent=2), encoding="utf-8")
            except OSError:
                pass  # the next turn starts a new session; nothing else is lost
        where = f"Steps and approvals: the '{title}' session."
        if result.stopped == "timeout":
            return (f"Still working after {int(AUTO_TIMEOUT // 60)} minutes. It carries on in the '{title}' "
                    f"session: open it to watch (approve anything it's waiting on there). ({how})")
        if result.error or not result.text:
            return f"Couldn't finish: {result.error or 'the model returned nothing'}. ({how} {where})"
        return f"{result.text}\n\n({how} {where})"

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
        first; a job another server (another Desktop window on this project) is running makes this
        one wait for the user to ask again, so two servers never work in the same folder at once."""
        folder = store.path.parent
        job = store.get_job(job_id)
        what = f"{job.kind} for {job.target} (job {job.id})"
        with self._running:
            other = runstate.running_pid(folder)
            if other and other != os.getpid():
                store.cancel_job(job_id)
                return (f"Not started: another BharatCode Desktop window is running a spare-gpu job in this project "
                        f"(process {other}). Ask again once it's done; spare_work_status shows it.")
            for left in runstate.tidy(store):  # jobs a crashed server left running: nothing resumes them
                self._drop_copy(store, left)
            try:
                job_config = JobConfig.load(Path(job.repo))
                budgets = Budgets.load(Path(job.repo))
            except ValueError as exc:
                store.cancel_job(job_id)
                return f"Not started: {exc}"
            try:
                worker = self._make_worker(store, progress, job_config)
            except desktop.DesktopNotFound as exc:
                store.cancel_job(job_id)
                return f"Not started: {what} runs as a BharatCode Desktop session, and Desktop wasn't reached ({exc})."
            runstate.clear(folder)
            runstate.write_run(folder)
            try:
                scheduler = Scheduler(
                    store, agent=worker.agent, plans=self._plans, budgets=budgets,
                    config=job_config, log=progress, sleep=self._sleep,
                    should_stop=lambda: store.get_job(job_id).status == "cancelled",
                )
                status = scheduler.run_job(job_id)
            finally:
                runstate.clear(folder)
                worker.close()
            if status == "paused":  # stopped before it finished: nothing resumes it, so its copy goes
                self._drop_copy(store, job_id)
        return self._outcome(store, store.get_job(job_id), status)

    @staticmethod
    def _drop_copy(store: Store, job_id: str) -> None:
        """Delete a stopped job's private copy of the project (best effort: a locked file keeps it)."""
        try:
            worktree.remove(Path(store.get_job(job_id).repo), job_id)
        except (worktree.GitError, OSError):
            pass

    def _outcome(self, store: Store, job, status: str) -> str:
        what = f"{job.kind} for {job.target} (job {job.id})"
        tokens = store.request_summary(job.id)["total_tokens"]
        spent = f" It used {tokens:,} tokens." if tokens else ""
        where = " Its full transcript is the Desktop session named \"spare-gpu: ...\"."
        if status == "done":
            if job.output_dir:
                return (f"Done: {what} finished; its edited files are in {job.output_dir}/ in this project. Your "
                        f"files weren't touched: /apply copies them in, /undo takes that back.{spent}{where}")
            return f"Done: {what} finished, but it changed no files.{spent}{where}"
        if status == "failed":
            step = next((s for s in store.steps(job.id) if s["status"] == "failed"), None)
            why = f": {step['output']}" if step and step["output"] else ""
            return f"Failed: {what}{why}.{spent}{where}".replace("..", ".")
        if status == "cancelled":
            return f"Cancelled: {what} was stopped; nothing was copied to {worktree.OUTPUT_DIR}/.{spent}"
        try:
            used = Budgets.load(Path(job.repo)).per_job
        except ValueError:
            used = Budgets().per_job
        return (f"Stopped: {what} stopped before it finished"
                + (f" (its token budget of {used:,} is used up; raise job_token_budget in .gtu/settings.json)"
                   if tokens >= used else " (a stop was asked for)")
                + f". Nothing was copied to {worktree.OUTPUT_DIR}/; ask for the task again to run it afresh.{spent}")


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

    def collect_sources(self, folder: str) -> str:
        root = self._root()
        path = Path(folder.strip() or ".")
        path = (path if path.is_absolute() else root / path).resolve()
        if root not in path.parents and path != root:
            return f"Didn't collect them: {folder} is outside this project ({root})."
        try:
            _, summary = refs.collect_sources(path)
        except FileNotFoundError:
            return f"No notes files in {path / refs.NOTES_DIR}: the scouts write them there."
        return summary

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

    def tally_findings(self, findings: str) -> str:
        try:
            candidates = tally.parse(findings)
        except ValueError as exc:
            return f"Couldn't read the candidates: {exc}."
        return tally.describe(tally.tally(candidates))

    # --- what /apply, /undo, /cost, /prompts and /doctor call ---------------------------------------

    def apply_result(self, stamp: str = "latest", dry_run: bool = False, force: bool = False) -> str:
        from . import landing

        stamp = (stamp or "").strip() or "latest"
        try:
            outcome = landing.apply(landing.find_root(self.base), stamp, dry_run=dry_run, force=force)
        except landing.LandingError as exc:
            return f"Didn't apply it: {exc}"
        except OSError as exc:
            return f"Stopped part way: {exc}. What was applied before this is recorded, so /undo {stamp} still works."
        return "\n".join(outcome.lines)

    def undo_result(self, stamp: str = "latest") -> str:
        from . import landing

        stamp = (stamp or "").strip() or "latest"
        try:
            outcome = landing.undo(landing.find_root(self.base), stamp)
        except landing.LandingError as exc:
            return f"Didn't undo it: {exc}"
        except OSError as exc:
            return (f"Stopped part way: {exc}. The remaining files are still in spare-gpu/<stamp>/.undo; "
                    f"run /undo {stamp} again.")
        return "\n".join(outcome.lines)

    @staticmethod
    def _channel(channel: str | None) -> str | None:
        """A Desktop channel the user named, checked; None: the Desktop that started this server."""
        from .bc_config import CHANNELS

        channel = (channel or "").strip().lower() or None
        if channel is not None and channel not in CHANNELS:
            raise ValueError(f"unknown Desktop build {channel!r}; use one of {', '.join(CHANNELS)}")
        return channel

    def cost_summary(self, days: float = 1.0, channel: str | None = None, short: bool = True) -> str:
        from . import costs

        if days <= 0:
            return "Give a period of more than 0 days."
        try:
            channel = self._channel(channel)
        except ValueError as exc:
            return str(exc)
        path = db_path(self._root())
        with Store(path if path.exists() else ":memory:") as store:
            summary = costs.period(store, days=days, channel=channel)
        return costs.render_short(summary) if short else costs.render(summary)

    def prompt_rules(self, days: int = 30, min_sessions: int = 3, channel: str | None = None) -> str:
        from . import bc_config, prompts as prompting

        if days < 1 or min_sessions < 1:
            return "The number of days and the minimum sessions must each be at least 1."
        try:
            channel = self._channel(channel) or stats.channel()
        except ValueError as exc:
            return str(exc)
        folder = bc_config.config_dir() if channel == "stable" else bc_config.channel_config_dir(channel)
        try:
            review = prompting.collect(prompting.database_path(channel), days=days, min_sessions=min_sessions)
        except prompting.PromptsError as exc:
            return str(exc)
        return prompting.render(review, bc_config.config_file(folder))

    def doctor_check(self, live: bool = False, channel: str | None = None) -> str:
        from . import doctor as doctoring

        try:
            channel = self._channel(channel) or stats.channel()
        except ValueError as exc:
            return str(exc)
        report = doctoring.run(self.base, channel=channel, live_mode=live)
        lines = [str(finding) for finding in report.findings] + [f"Result: {report.verdict}"]
        lines.append(f"Still manual: {doctoring.MANUAL_LEFT}." if live else
                     "/doctor live also measures a real Desktop session (one short model call).")
        return "\n".join(lines)


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

        Use it for any "where are things" question: "what's queued?", "is my fix job done?",
        "what is spare-gpu doing?", "why hasn't my job started?", "what did spare-gpu do
        today?". Also use it to find a job id before cancelling. For how long each model would
        wait right now use spare_waits; for what the day cost, the /cost command."""
        return tools.work_status()

    @server.tool(annotations=notes(False, destructive=True))
    def spare_cancel_work(job_id: str) -> str:
        """Cancel a queued or running spare-gpu job. Nothing of a cancelled job is copied to the
        spare-gpu/ folder.

        Use it for "cancel the fix job", "drop job 1a2b3c4d", "stop that job".

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

    @server.tool(annotations=notes(False, world=True))
    async def spare_auto(request: str, your_model: str | None = None, new: bool = False,
                         ctx: Context | None = None) -> str:
        """Do the user's request exactly as a normal BharatCode chat would (read and edit files, run
        commands, search, use tools), on whichever BharatCode model will start it sooner, so it
        doesn't sit in DeepSeek's queue. It runs in a background Desktop session in this project
        that keeps the conversation: each follow-up from this chat continues it, and the model is
        picked again every turn. Waits for the reply and returns it, with which model answered and
        why. Approvals the work needs are asked in that session; the reply says so.

        Use it for anything the user sends through /auto: "/auto what does calc.py do?", "/auto add
        a divide function", "ask whichever model is free: ...". Pass the reply on as it comes back.
        It is not spare_delegate, which only /code-review --fix and /simplify use for their fix jobs.

        Args:
            request: The user's request, exactly as written.
            your_model: The model the request would otherwise go to, e.g. "deepseek-v4.1-flash".
            new: True to start a new conversation ("/auto new ...") instead of continuing.
        """
        return await run_with_progress(ctx, lambda progress: tools.auto(request, your_model, new, progress))

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
        def collect_sources(folder: str) -> str:
            """Merge the scouts' sources for /deep-research: reads the ```sources list in every
            <folder>/notes/*.md and writes <folder>/sources.json. A source two scouts found (same
            DOI, arXiv id, OpenAlex id or URL) becomes one entry and both keys keep working in
            citations; two different sources under one key get the later renamed, in its notes file
            too. Says how many were merged and the quality mix (A primary, B secondary, C weak).

            Args:
                folder: The research folder, e.g. "research/rag-evaluation".
            """
            return tools.collect_sources(folder)

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
        def tally_findings(findings: str) -> str:
            """Check the finders' candidates of /code-review and /security-review before the verifier
            reads them: drop entries that aren't findings (no file, line or claim), flag candidates
            whose text addresses the reader instead of describing the code (a quoted comment or page
            trying to steer the review; drop them unread), and merge candidates about the same file
            and line with how many finders agreed, most agreed first. Judges nothing about the code.

            Args:
                findings: The finders' answers pasted together: JSON lists of objects with file, line,
                    claim or description, and optionally failure scenario, severity, category and
                    confidence (0 to 1, or high/medium/low). Code fences are fine.
            """
            return tools.tally_findings(findings)

        @server.tool(annotations=notes(False, destructive=True))
        def apply_result(stamp: str = "latest", dry_run: bool = False, force: bool = False) -> str:
            """/apply: copy a finished job's files from spare-gpu/<stamp>/ into the user's project. A
            file that changed since the job started is skipped and said so (force replaces it
            anyway). What was there before is saved under spare-gpu/<stamp>/.undo/, so undo_result
            can take it all back.

            Args:
                stamp: The result folder under spare-gpu/, e.g. 2026-11-05_12-38-50, or "latest".
                dry_run: Say what would happen; write nothing.
                force: Also replace files that changed since the job started.
            """
            return tools.apply_result(stamp, dry_run, force)

        @server.tool(annotations=notes(False, destructive=True))
        def undo_result(stamp: str = "latest") -> str:
            """/undo: take back an apply_result: restore every file it replaced, remove the files it
            added, and remove the .undo folder, so the result can be applied again.

            Args:
                stamp: The result folder that was applied, or "latest": the newest one applied.
            """
            return tools.undo_result(stamp)

        @server.tool(annotations=notes(True))
        def cost_summary(days: float = 1.0, channel: str | None = None, short: bool = True) -> str:
            """/cost: what a period cost. Tokens per model from BharatCode Desktop's own record of every
            reply on this machine, then this project's spare-gpu jobs: how many ran and how they
            ended, their tokens, how many kept nothing after their check, and the most expensive.

            Args:
                days: How many days back (1 = today's last 24 hours, 7 = a week).
                channel: Which Desktop build's record: stable, beta, dev or local (default: the one
                    running this chat).
                short: One paragraph (default); false gives the tables.
            """
            return tools.cost_summary(days, channel, short)

        @server.tool(annotations=notes(True))
        def prompt_rules(days: int = 30, min_sessions: int = 3, channel: str | None = None) -> str:
            """/prompts: allow rules for the read-only commands the user keeps approving in Desktop.
            Reads a private copy of Desktop's database, counts the sessions each command was
            approved in, keeps those on a short list of commands that only read (git status, ls,
            pytest, ...), and returns the "permission" snippet to paste into the config file it
            names. Writes nothing.

            Args:
                days: How many days of sessions to look at.
                min_sessions: Propose a command only if it was approved in at least this many sessions.
                channel: Which Desktop build: stable, beta, dev or local (default: the one running this chat).
            """
            return tools.prompt_rules(days, min_sessions, channel)

        @server.tool(annotations=notes(False, world=True))
        def doctor_check(live: bool = False, channel: str | None = None) -> str:
            """/doctor: check the whole install and say what to fix: spare-gpu, git, Desktop's config,
            the MCP entry (and that the server really starts), the pack, this project's jobs and
            settings, each finding PASS, WARN or FAIL with its fix.

            Args:
                live: Also run one tiny real request as a Desktop session (one short model call) to
                    measure the per-step context and check a job's copy.
                channel: Which Desktop build's config: stable, beta, dev or local (default: the one
                    running this chat).
            """
            return tools.doctor_check(live, channel)

        @server.tool(annotations=notes(True))
        def pack_cost() -> str:
            """The tokens each model used in this project's Desktop sessions since the spare_waits
            call that started the pack command, exact, from Desktop's own record of every reply: for
            the cost summary at the end of /deep-research, /code-review, /security-review and
            /simplify. When it has no figures, say so; never estimate them."""
            return tools.pack_cost()

    @server.tool(annotations=notes(False, world=True))
    async def spare_delegate(type: Literal["fix"], path: str,
                             note: str | None = None, your_model: str | None = None, ctx: Context | None = None) -> str:
        """Apply a list of review findings to this repo now, as a fix job: a BharatCode Desktop
        session the user can watch, in a private copy of the project. The result is kept only if
        the project's test command passes with it; changes to existing tests are reverted first.
        The call returns when the job is done, with the result and where its edited files are:
        spare-gpu/<date>_<time>/ in the project. The user's own files are never touched, and the
        work itself never comes back into this chat.

        Use it for the accepted findings of /code-review --fix and /simplify. For anything else (a
        question, a change made right in the project) use spare_auto.

        Args:
            type: "fix", the only job type.
            path: The folder the fix works on, relative to the repo root: "." for the whole repo.
                Pass it exactly as given; spare-gpu says if it doesn't exist.
            note: The findings to apply, one per line with file:line and the change wanted.
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
