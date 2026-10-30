from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import typer

from . import __version__
from .api import API_KEY_ENV, ApiError, ApiKeyMissing, get_api_key, list_models, verify_key
from .client import DEFAULT_MODEL, AuthError, BharatCodeClient, BharatCodeError, BusyError, RetryPolicy
from .demo import DEMO_RETRY, DEMO_TIMEOUT, pick_file, run_demo
from .gate import Gate, GateConfig
from .agent_cli import BharatCodeCli
from .budget import Budgets
from .jobs import JobConfig
from .scheduler import Scheduler
from . import dryrun, runstate, worktree
from . import report as reporting
from .store import Store, StoreError

SET_KEY = f'Set it in PowerShell with `$env:{API_KEY_ENV} = "<your key>"`'


def _utf8_when_redirected() -> None:
    """On Windows, output sent to a file or pipe uses the ANSI code page (cp1252), which
    can't encode the check marks and emoji models like to write: `gtu demo > out.txt`
    would crash with UnicodeEncodeError. Consoles are fine (Python writes Unicode to them
    directly), so only redirected streams switch to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # a replaced stream (tests, embedding) that can't be reconfigured


_utf8_when_redirected()


def _local(stamp: str) -> str:
    """A stored UTC timestamp as the user's local time, e.g. '30 Oct 11:24'."""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return stamp

app = typer.Typer(
    help="Queue repo chores (tests, docs, code review) and run them on BharatCode's spare capacity. "
         "New here? Run `gtu demo`.",
    no_args_is_help=True,
    add_completion=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show version."),
) -> None:
    pass


@app.command()
def check() -> None:
    """Call GET /models with the key from the environment and report what is available."""
    try:
        result = list_models()
    except ApiKeyMissing:
        typer.echo(f"error: {API_KEY_ENV} is not set. {SET_KEY} and try again.", err=True)
        raise typer.Exit(1)
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"BharatCode API reachable in {result.elapsed_ms:.0f} ms (GET /models)")
    typer.echo("Models:")
    for model in result.models:
        tier = f" [{model.access_tier}]" if model.access_tier else ""
        name = f" - {model.display_name}" if model.display_name else ""
        typer.echo(f"  {model.id}{name}{tier}")

    # GET /models does not check the key, so confirm it with a 1-token request.
    try:
        key_check = verify_key(result.models)
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    tokens = f", {key_check.total_tokens} tokens used" if key_check.total_tokens is not None else ""
    typer.echo(f"API key accepted: 1-token test on {key_check.model} took {key_check.elapsed_ms:.0f} ms{tokens}")
    typer.echo(f"x-request-id: {key_check.request_id or 'not sent by /chat/completions'}")


class JobKind(str, Enum):
    tests = "tests"
    docs = "docs"
    review = "review"


def _fail(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def add(
    kind: JobKind = typer.Argument(..., help="What to do: tests, docs or review."),
    target: Path = typer.Argument(..., help="File or folder in this repo to work on."),
    note: str = typer.Option(None, "--note", help="Extra instructions for the job."),
) -> None:
    """Queue a job. It runs later with `gtu start`, on its own branch gtu/<job-id>."""
    try:
        repo = worktree.repo_root(Path.cwd()).resolve()
        worktree.ensure_clean(repo)
    except worktree.GitError as exc:
        _fail(str(exc))
    path = (Path.cwd() / target).resolve()
    if not path.exists():
        _fail(f"{target} does not exist")
    try:
        relative = path.relative_to(repo)
    except ValueError:
        _fail(f"{target} is outside this repo ({repo})")
    with Store.open_default() as store:
        job = store.add_job(kind.value, relative.as_posix() or ".", str(repo), note)
    typer.echo(f"Queued job {job.id}: {job.kind} {job.target}")


@app.command("list")
def list_cmd(
    active: bool = typer.Option(False, "--active", help="Hide done, failed and cancelled jobs."),
) -> None:
    """Show queued jobs."""
    with Store.open_default() as store:
        jobs = store.list_jobs(include_finished=not active)
    if not jobs:
        typer.echo("No jobs.")
        return
    typer.echo(f"{'ID':<10}{'KIND':<8}{'STATUS':<11}{'CREATED':<14}TARGET")
    for job in jobs:
        typer.echo(f"{job.id:<10}{job.kind:<8}{job.status:<11}{_local(job.created_at):<14}{job.target}")


@app.command()
def status(job_id: str = typer.Argument(..., help="Job id from `gtu list`.")) -> None:
    """Show one job, its steps and its request log totals."""
    with Store.open_default() as store:
        try:
            job = store.get_job(job_id)
        except StoreError as exc:
            _fail(str(exc))
        steps = store.steps(job_id)
        summary = store.request_summary(job_id)
    typer.echo(f"Job {job.id}: {job.kind} {job.target}")
    typer.echo(f"  status:   {job.status}")
    typer.echo(f"  created:  {_local(job.created_at)}   updated: {_local(job.updated_at)}")
    if job.note:
        typer.echo(f"  note:     {job.note}")
    typer.echo(f"  steps:    {len(steps)}")
    for step in steps:
        typer.echo(f"    {step['seq']}. {step['name']} [{step['status']}]")
    typer.echo(
        f"  requests: {summary['requests']} ({summary['busy']} busy, {summary['truncated']} truncated), "
        f"{summary['total_tokens']} tokens"
    )
    if summary["last_request_id"]:
        typer.echo(f"  last x-request-id: {summary['last_request_id']}")


@app.command()
def demo(
    file: Path = typer.Argument(None, help="File to review. Default: a small source file picked for you."),
) -> None:
    """See it work in a minute or two: one short code review, sent straight away."""
    try:
        get_api_key()
    except ApiKeyMissing:
        _fail(f"{API_KEY_ENV} is not set. Get a key at https://bharatcode.ai/account. "
              f"{SET_KEY}, then run `gtu demo` again.")
    try:
        root = worktree.repo_root(Path.cwd()).resolve()
    except worktree.GitError:
        root = Path.cwd().resolve()
    target = (Path.cwd() / file).resolve() if file else pick_file(root)
    if target is None:
        _fail("found no small source file here to review; try `gtu demo path/to/file.py`")
    if not target.is_file():
        _fail(f"{file} is not a file")
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        _fail(f"{file} is outside {root}")

    typer.echo(f"Reviewing {rel} ({target.stat().st_size} bytes) with BharatCode's {DEFAULT_MODEL}.")
    typer.echo("The demo skips the capacity gate and sends one request now. This usually takes "
               "under a minute, but can take two or more when BharatCode is busy.\n")
    with Store.open_default() as store:
        client = BharatCodeClient(store=store, timeout=DEMO_TIMEOUT, retry=DEMO_RETRY,
                                  on_retry=lambda message: typer.echo(message))
        try:
            outcome = run_demo(root, target, client)
        except AuthError:
            _fail(f"BharatCode rejected the key in {API_KEY_ENV}. Check it at https://bharatcode.ai/account.")
        except BusyError:
            _fail("BharatCode is busy right now. Try `gtu demo` again in a minute.")
        except BharatCodeError as exc:
            _fail(str(exc))
        finally:
            client.close()

    result = outcome.result
    rule = "-" * 72
    typer.echo(rule)
    typer.echo(result.content.strip())
    typer.echo(rule)
    stats = f"{result.usage.total_tokens} tokens, {result.elapsed_ms / 1000:.1f} s"
    if result.queue_wait_ms is not None:
        stats += f" (of which {result.queue_wait_ms / 1000:.1f} s waiting in BharatCode's queue)"
    typer.echo(stats + (f", request {result.request_id}" if result.request_id else "") + ".")
    if result.truncated:
        typer.echo("The answer was cut off at the demo's short limit; real review jobs allow much longer answers.")
    typer.echo(f"Saved to {outcome.saved_to}\n")
    typer.echo(
        "That was one request, sent immediately. Real jobs run on their own git branch and are sent\n"
        "only when BharatCode has spare capacity, so they may not start straight away:\n\n"
        f"  gtu add review {rel}   # a full review, saved as gtu-review-<id>.md\n"
        f"  gtu add tests {rel}    # tests for it; only ones that pass are kept\n"
        f"  gtu add docs {rel}     # docstrings and README additions\n"
        "  gtu start                # runs them when there is capacity\n"
        "  gtu why                  # what it's waiting for, if anything"
    )


@app.command()
def why() -> None:
    """Show whether the capacity gate would send work now, and why."""
    with Store.open_default() as store:
        gate = Gate(store, GateConfig.from_env())
        try:
            decision = gate.check(allow_probe=False)
        finally:
            gate.close()
    typer.echo(decision.describe())


@app.command()
def start(
    max_steps: int = typer.Option(None, "--max-steps", help="Stop after this many completed steps."),
    quiet_readings: int = typer.Option(
        None, "--quiet-readings", min=1, help="Override: quiet readings in a row needed to open the gate (default 3)."
    ),
    max_queued: int = typer.Option(
        None, "--max-queued", min=0, help="Override: queue length still counted as quiet (default GTU_MAX_QUEUED or 5)."
    ),
    now: bool = typer.Option(
        False, "--now", help="Ignore the capacity gate and run queued jobs immediately."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show which jobs and steps would run and roughly what they'd cost; send nothing."
    ),
) -> None:
    """Run queued jobs one step at a time while BharatCode has spare capacity (or right away with --now)."""

    def log(message: str) -> None:
        typer.echo(f"[{datetime.now():%H:%M:%S}] {message}")

    if dry_run:
        try:
            job_config = JobConfig.from_env()
        except ValueError as exc:
            _fail(str(exc))
        with Store.open_default() as store:
            typer.echo(dryrun.describe(store, Budgets.from_env(), job_config, datetime.now(timezone.utc), now))
        return

    if now:
        typer.echo(
            "--now: running queued jobs immediately, without waiting for spare capacity.\n"
            "Normally `gtu start` sends work only when BharatCode has a free slot, so your jobs use\n"
            "capacity nobody else needs. With --now they compete with everyone else's requests and may\n"
            "wait in BharatCode's queue. Busy answers are still retried politely (up to ~15 min), and\n"
            "token budgets still apply.\n"
        )
    with Store.open_default() as store:
        folder = store.path.parent
        other = runstate.running_pid(folder)
        if other:
            _fail(f"`gtu start` is already running (process {other}). Use `gtu stop` to stop it first.")
        try:
            # Gated: no client-side retries; a busy answer closes the gate, which decides when to try
            # again. --now: no gate to do that, so the client's patient retry ladder takes over.
            client = BharatCodeClient(store=store, retry=RetryPolicy.from_env() if now else RetryPolicy(max_retries=0),
                                      on_retry=lambda message: log(f"  {message}"))
        except ApiKeyMissing:
            _fail(f"{API_KEY_ENV} is not set. {SET_KEY} and try again.")
        config = GateConfig.from_env()
        if quiet_readings is not None:
            config.quiet_needed = quiet_readings
        if max_queued is not None:
            config.max_queued = max_queued
        gate = Gate(store, config, client=client)
        runstate.clear(folder)  # a stop request left over from an earlier run doesn't apply to this one
        runstate.write_run(folder)
        try:
            job_config = JobConfig.from_env()
            scheduler = Scheduler(
                store, gate, client, cli=BharatCodeCli(timeout=job_config.cli_timeout),
                budgets=Budgets.from_env(), config=job_config, log=log, ignore_gate=now,
                should_stop=lambda: runstate.stop_requested(folder),
            )
            steps = scheduler.run(max_steps=max_steps)
        except AuthError as exc:
            _fail(str(exc))
        except KeyboardInterrupt:
            paused = runstate.tidy(store)
            typer.echo("\nStopped." + (f" Paused job(s) {', '.join(paused)}." if paused else "")
                       + " Progress is saved; run `gtu start` again to resume.")
            raise typer.Exit(130)
        finally:
            runstate.clear(folder)
            gate.close()
            client.close()
    if scheduler.stopped:
        return
    log(f"Completed {steps} step(s).")


@app.command()
def stop(
    wait: float = typer.Option(10.0, "--wait", min=0, help="Seconds to wait for a clean stop before killing it."),
) -> None:
    """Stop a running `gtu start` now. Jobs are paused, not failed; `gtu start` resumes them."""
    with Store.open_default() as store:
        folder = store.path.parent
        pid = runstate.running_pid(folder)
        if pid is None:
            runstate.clear(folder)
            paused = runstate.tidy(store)  # e.g. after a crash: don't leave jobs marked running
            typer.echo("Nothing is running." + (f" Paused interrupted job(s): {', '.join(paused)}." if paused else ""))
            return
        runstate.request_stop(folder)
        typer.echo(f"Asked `gtu start` (process {pid}) to stop...")
        if runstate.wait_for_exit(pid, wait):
            typer.echo("It stopped cleanly.")
        elif runstate.kill_tree(pid):
            typer.echo(f"It didn't stop within {wait:g} s, so it and its BharatCode CLI process were killed.")
        else:
            _fail(f"process {pid} doesn't look like gtu, so it was not killed; stop it yourself.")
        runstate.clear(folder)
        paused = runstate.tidy(store)
        jobs = store.list_jobs(include_finished=False)
    held = [j.id for j in jobs if j.status == "paused"]
    if paused or held:
        typer.echo(f"Paused job(s): {', '.join(sorted(set(paused + held)))}. "
                   "Unfinished edits were discarded; finished steps are kept on each job's branch.")
    typer.echo("Run `gtu start` to resume.")


@app.command()
def report(
    since: float = typer.Option(None, "--since", help="Hours to look back. Default: since tonight began (12:00 IST)."),
    job: str = typer.Option(None, "--job", help="Report on one job only."),
    all_jobs: bool = typer.Option(False, "--all", help="Every job ever run."),
) -> None:
    """Morning summary: what ran, tokens, what it produced, and the capacity it was sent into."""
    now = datetime.now(timezone.utc)
    budgets = Budgets.from_env()
    if all_jobs or job:
        start_from = None
    else:
        start_from = now - timedelta(hours=since) if since else budgets.night_began(now)
    with Store.open_default() as store:
        try:
            reports = [reporting.build_job(store, j) for j in reporting.select_jobs(store, start_from, job)]
        except StoreError as exc:
            _fail(str(exc))
        written: dict[str, str | None] = {}
        for r in reports:
            try:
                written[r.job.id] = reporting.write_to_branch(r)
            except worktree.GitError as exc:
                typer.echo(f"warning: could not write the report to {worktree.branch_name(r.job.id)}: {exc}", err=True)
        text = reporting.summary_markdown(reports, store, budgets, now, start_from, written)
        out = store.path.parent / "reports" / f"report-{now.astimezone(reporting.IST):%Y%m%d-%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(text)
    typer.echo(f"Saved to {out}")


@app.command()
def cancel(job_id: str = typer.Argument(..., help="Job id from `gtu list`.")) -> None:
    """Cancel a job that has not finished."""
    with Store.open_default() as store:
        try:
            job = store.cancel_job(job_id)
        except StoreError as exc:
            _fail(str(exc))
    try:
        worktree.remove(Path(job.repo), job.id)
    except worktree.GitError as exc:
        typer.echo(f"warning: could not remove the job's worktree: {exc}", err=True)
    typer.echo(f"Cancelled job {job.id}. Anything it committed stays on branch {worktree.branch_name(job.id)}.")
