from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

import typer

from . import __version__
from .api import API_KEY_ENV, ApiError, ApiKeyMissing, list_models, verify_key
from .client import AuthError, BharatCodeClient, RetryPolicy
from .gate import Gate, GateConfig
from .scheduler import Scheduler
from . import worktree
from .store import Store, StoreError

app = typer.Typer(
    help="Queue repo work and run it overnight on BharatCode's spare capacity.",
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
        typer.echo(f"error: {API_KEY_ENV} is not set. Export it and try again.", err=True)
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
    typer.echo(f"{'ID':<10}{'KIND':<8}{'STATUS':<11}{'CREATED':<27}TARGET")
    for job in jobs:
        typer.echo(f"{job.id:<10}{job.kind:<8}{job.status:<11}{job.created_at:<27}{job.target}")


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
    typer.echo(f"  created:  {job.created_at}   updated: {job.updated_at}")
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
) -> None:
    """Run queued jobs one step at a time while BharatCode has spare capacity."""

    def log(message: str) -> None:
        typer.echo(f"[{datetime.now():%H:%M:%S}] {message}")

    with Store.open_default() as store:
        try:
            # No client-side retries: a busy answer closes the gate, which decides when to try again.
            client = BharatCodeClient(store=store, retry=RetryPolicy(max_retries=0))
        except ApiKeyMissing:
            _fail(f"{API_KEY_ENV} is not set. Export it and try again.")
        config = GateConfig.from_env()
        if quiet_readings is not None:
            config.quiet_needed = quiet_readings
        if max_queued is not None:
            config.max_queued = max_queued
        gate = Gate(store, config, client=client)
        try:
            steps = Scheduler(store, gate, client, log=log).run(max_steps=max_steps)
        except AuthError as exc:
            _fail(str(exc))
        except KeyboardInterrupt:
            typer.echo("\nStopped. Progress is saved; run `gtu start` again to resume.")
            raise typer.Exit(130)
        finally:
            gate.close()
            client.close()
    log(f"Completed {steps} step(s).")


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
