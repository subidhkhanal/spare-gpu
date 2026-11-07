from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import httpx
import typer

from . import __version__
from .api import (API_KEY_ENV, ApiError, ApiKeyMissing, KeyRejected, clean_key, find_api_key,
                  get_api_key, key_problem, list_models, verify_key)
from . import keystore
from .client import DEFAULT_MODEL, AuthError, BharatCodeClient, BharatCodeError, BusyError
from .demo import DEMO_RETRY, DEMO_TIMEOUT, pick_file, run_demo
from .budget import Budgets
from .jobs import JobConfig
from .scheduler import Scheduler
from . import actions, bc_config, dryrun, pack, router, runstate, waits, workers, worktree
from . import report as reporting
from .store import Store, StoreError

GET_KEY_URL = "https://bharatcode.ai/account"


def _no_key(exc: Exception) -> str:
    """The one message for a missing key: where to get one and exactly how to set it."""
    return (
        f"{exc}.\n"
        f"  1. Get a key at {GET_KEY_URL}\n"
        "  2. Run:  gtu setkey\n"
        "     It asks for the key and saves it for your Windows account, so every terminal finds it.\n"
        f'  Or, for this terminal only (PowerShell):  $env:{API_KEY_ENV} = "<your key>"'
    )


def _rejected_key() -> str:
    found = find_api_key()
    source = f", which came from {found[1]}" if found else ""
    return (f"BharatCode rejected your API key{source}. Copy it again from {GET_KEY_URL} "
            "and save it with `gtu setkey`.")


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
    help="Queue repo chores (tests, docs, code review) and run them unattended through BharatCode, each finished job's edited files copied to spare-gpu/<date>_<time>/. "
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
def setup(
    remove: bool = typer.Option(False, "--remove", help="Unregister spare-gpu from BharatCode Desktop."),
    check_key: bool = typer.Option(True, "--check/--no-check",
                                   help="Test a newly entered key with one 1-token request."),
) -> None:
    """Set everything up in one go: your API key, and spare-gpu's MCP server in BharatCode Desktop.
    Safe to run again."""
    if remove:
        _uninstall()
        return

    typer.echo("Setting up spare-gpu.\n")
    typer.echo("1/3  Your BharatCode API key")
    try:
        found = find_api_key()
    except ApiKeyMissing:
        found = None
    if found:
        typer.echo(f"     Found in {found[1]}.")
    elif not keystore.supported():
        _fail(f"set the {API_KEY_ENV} environment variable, then run `gtu setup` again.")
    else:
        _read_check_save_key(check_key)

    typer.echo("\n2/3  BharatCode Desktop (where you'll talk to spare-gpu)")
    _report_desktop("     ")

    typer.echo("\n3/3  spare-gpu's MCP server in BharatCode Desktop")
    if not _register("     "):
        raise typer.Exit(1)
    typer.echo("\nDone. Open BharatCode in one of your projects and ask, for example:")
    for phrase in EXAMPLE_PHRASES:
        typer.echo(f'  "{phrase}"')
    typer.echo("Already-open BharatCode windows need a restart to see spare-gpu.")


@app.command()
def integrate(
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove spare-gpu from BharatCode's config "
                                                              "(with --pack: only the pack)."),
    channel: str = typer.Option(
        "all", "--channel",
        help="Which BharatCode Desktop to register with: stable (the released app), beta, dev "
             "(a Desktop build from source), local, or all (every one installed here; the default).",
    ),
    with_pack: bool = typer.Option(False, "--pack", help="Also install the command pack: /deep-research, "
                                                         "/code-review, ... and the model split."),
    only: str = typer.Option(None, "--only", help="Pack components, comma-separated: "
                                                  + ", ".join(pack.COMPONENTS) + " (default: all)."),
    lite: bool = typer.Option(False, "--lite", help="Qwen-only plan: every pack role runs on the standard model."),
) -> None:
    """Register spare-gpu's MCP server with BharatCode Desktop. Backs each config up
    first, says exactly what it changed and how to undo it, and is safe to run again."""
    if channel != "all" and channel not in bc_config.CHANNELS:
        _fail(f"unknown channel {channel!r}; use one of {', '.join(bc_config.CHANNELS)} or all.")
    channels = bc_config.installed_channels() if channel == "all" else [channel]
    if uninstall:
        for name in channels:
            if with_pack:
                _uninstall_pack(name)
            else:
                _uninstall(name)
        return
    if with_pack:
        try:
            pack.select(only)
        except pack.PackError as exc:
            _fail(str(exc))
    ok = True
    for name in channels:
        typer.echo(f"BharatCode {name}:")
        ok = _register_channel("  ", name, with_pack) and ok
        if with_pack:
            ok = _install_pack("  ", name, only, lite) and ok
    typer.echo("Restart BharatCode Desktop (and close open BharatCode windows) to load spare-gpu.")
    if not ok:
        raise typer.Exit(1)


def _channel_folder(channel: str) -> tuple[Path, str]:
    if channel == "stable":
        return bc_config.locate_config_dir()
    return bc_config.channel_config_dir(channel), f"BharatCode Desktop's {channel} channel"


def _install_pack(pad: str, channel: str, only: str | None, lite: bool) -> bool:
    """Install the pack into one channel: its config file, then its commands and agents with the
    model ids filled in. Says what changed, what conflicts with the user's own settings, and how to undo."""
    folder, _ = _channel_folder(channel)
    components = pack.select(only)
    models, note = pack.resolve_models(lite=lite)
    typer.echo(f"{pad}Pack models: heavy {waits.name(models.heavy)} ({models.heavy}), "
               f"standard {waits.name(models.standard)} ({models.standard}){note}")
    doc = pack.config(components, models)
    try:
        path, outcome = bc_config.install_pack_config(folder, pack.config_text(doc, bc_config.PACK_LINE))
    except (bc_config.ConfigError, OSError) as exc:
        typer.echo(f"{pad}Couldn't write the pack's settings: {exc}")
        return False
    said = {"added": "Wrote", "updated": "Updated", "unchanged": "Already up to date:"}[outcome]
    typer.echo(f"{pad}{said} the pack's settings in {path}: {pack.describe(doc)}.")
    files = pack.install_files(components, models)
    try:
        outcomes = bc_config.install_markdown(folder, files)
    except OSError as exc:
        typer.echo(f"{pad}Couldn't add the pack's commands and agents ({exc}).")
        return False
    for key, what in outcomes.items():
        kind, name = key.split("/", 1)
        shown = f"/{name}" if kind == "command" else f"the {name} agent"
        if what == "kept-yours":
            typer.echo(f"{pad}You already have {shown} ({folder / kind / (name + '.md')}); left it as it is.")
        elif what == "unchanged":
            typer.echo(f"{pad}{shown} is already there.")
        else:
            typer.echo(f"{pad}{'Added' if what == 'added' else 'Updated'} {shown} ({folder / kind / (name + '.md')}).")
    if not files:
        typer.echo(f"{pad}No commands or agents to add for: {', '.join(c.name for c in components)}.")
    for key in bc_config.remove_markdown(folder, list(pack.RETIRED)):  # left by an earlier version of the pack
        kind, name = key.split("/", 1)
        typer.echo(f"{pad}Removed {'/' + name if kind == 'command' else 'the ' + name + ' agent'}: no longer part of the pack.")
    for conflict in bc_config.pack_conflicts(folder, doc):
        typer.echo(f"{pad}Conflict: {conflict}.")
    typer.echo(f"{pad}To undo the pack: gtu integrate --pack --uninstall")
    return True


def _uninstall_pack(channel: str) -> None:
    folder, how = _channel_folder(channel)
    for key in bc_config.remove_markdown(folder, pack.all_markdown_names()):
        kind, name = key.split("/", 1)
        typer.echo(f"Removed {'/' + name if kind == 'command' else 'the ' + name + ' agent'} from {folder / kind}.")
    path, outcome = bc_config.remove_pack_config(folder)
    if outcome == "removed":
        typer.echo(f"Removed the pack's settings file {path} ({how}). Your own config was never changed.")
    else:
        typer.echo(f"The pack wasn't installed in {folder}; nothing changed.")
    try:
        if bc_config.registered_with_pack(bc_config.config_file(folder)):
            bc_config.register(bc_config.config_file(folder), bc_config.server_command())
            typer.echo("spare-gpu's MCP server no longer starts with --pack, so the pack's tools are gone from it.")
    except (bc_config.ConfigError, OSError) as exc:
        typer.echo(f"Couldn't take --pack off the spare-gpu MCP entry ({exc}); remove it by hand or run `gtu integrate`.")


def _register_channel(pad: str, channel: str, with_pack: bool = False) -> bool:
    """Register spare-gpu in one channel's config; Desktop checks the server connects when it starts."""
    where = bc_config.locate_config_dir() if channel == "stable" else \
        (bc_config.channel_config_dir(channel), f"BharatCode Desktop's {channel} channel")
    return _register(pad, where, with_pack)


def _register(pad: str, where: tuple[Path, str] | None = None, with_pack: bool = False) -> bool:
    """Register spare-gpu in BharatCode's config, saying what changed and how to undo it. When the
    file can't be edited safely, print the snippet to paste instead."""
    folder, how = where or bc_config.locate_config_dir()
    path = bc_config.config_file(folder)
    command = bc_config.server_command(with_pack)
    typer.echo(f"{pad}Config file: {path} ({how}).")
    backup = path.with_name(path.name + bc_config.BACKUP_SUFFIX)
    had_backup = backup.exists() and backup.stat().st_mtime
    try:
        outcome = bc_config.register(path, command)
    except (bc_config.ConfigError, OSError) as exc:
        typer.echo(f"{pad}Couldn't change it automatically: {exc}")
        typer.echo(f"{pad}Add this to {path} by hand (merge it into any existing \"mcp\" section):")
        typer.echo(bc_config.snippet(command))
        return False
    if outcome == "unchanged":
        typer.echo(f"{pad}spare-gpu is already registered there; nothing changed.")
    else:
        typer.echo(f"{pad}{'Added' if outcome == 'added' else 'Updated'} the \"spare-gpu\" entry under \"mcp\", "
                   f"which starts: {' '.join(command)}")
        if backup.exists() and backup.stat().st_mtime != had_backup:
            typer.echo(f"{pad}The file as it was before is saved as {backup}.")
        typer.echo(f"{pad}To undo: gtu integrate --uninstall" + (f" (or copy {backup.name} back)" if backup.exists() else ""))
    _install_commands(pad, folder)
    return True


def _install_commands(pad: str, folder: Path) -> None:
    try:
        outcome = bc_config.install_commands(folder)
    except OSError as exc:
        typer.echo(f"{pad}Couldn't add the /auto command ({exc}); everything else works without it.")
        return
    where = folder / "command"
    for name, what in outcome.items():
        if what == "kept-yours":
            typer.echo(f"{pad}You already have a /{name} command ({where / (name + '.md')}); left it as it is.")
        elif what == "unchanged":
            typer.echo(f"{pad}The /{name} command is already there.")
        else:
            typer.echo(f"{pad}{'Added' if what == 'added' else 'Updated'} the /{name} command ({where / (name + '.md')}): "
                       f"/{name} <task> hands the task to whichever model starts it sooner.")


def _uninstall(channel: str = "stable") -> None:
    folder, how = _channel_folder(channel)
    if any(bc_config.is_pack_file(folder / name) for name in bc_config.PACK_NAMES):
        _uninstall_pack(channel)
    path = bc_config.config_file(folder)
    for name in bc_config.remove_commands(folder):
        typer.echo(f"Removed the /{name} command from {folder / 'command'}.")
    try:
        removed = bc_config.unregister(path)
    except bc_config.ConfigError as exc:
        _fail(f"{exc}\nRemove the \"spare-gpu\" entry under \"mcp\" in {path} by hand.")
    if removed:
        typer.echo(f"Removed spare-gpu from {path} ({how}). The file as it was is saved as "
                   f"{path.name}{bc_config.BACKUP_SUFFIX}. Nothing else was changed; your jobs and key are kept.")
    else:
        typer.echo(f"spare-gpu wasn't registered in {path}; nothing changed.")


EXAMPLE_PHRASES = (
    "queue tests for src/utils tonight",
    "what did spare-gpu do last night?",
    "why hasn't my job started?",
)


def _report_desktop(pad: str = "") -> None:
    """Which BharatCode Desktop builds have a config folder here. Never fails: review jobs work
    without Desktop, given an API key."""
    found = [c for c in bc_config.installed_channels() if bc_config.channel_config_dir(c).is_dir()]
    if found:
        typer.echo(f"{pad}BharatCode Desktop: found ({', '.join(found)}). Jobs run as Desktop sessions while it's open.")
    else:
        typer.echo(f"{pad}BharatCode Desktop: no config folder found yet. Install it from bharatcode.ai and open it "
                   "once; spare-gpu is registered anyway, so it's there once Desktop is.")


@app.command()
def check() -> None:
    """Check your setup: BharatCode Desktop, your API key, and that the key works (one
    1-token request)."""
    _report_desktop()
    try:
        found = find_api_key()
    except ApiKeyMissing as exc:
        _fail(_no_key(exc))
    if found is None:
        _fail(_no_key(ApiKeyMissing("no BharatCode API key found")))
    key, source = found
    typer.echo(f"API key: found in {source}.")
    try:
        result = list_models(key=key)
    except ApiError as exc:
        _fail(str(exc))

    typer.echo(f"BharatCode reachable in {result.elapsed_ms:.0f} ms. Models:")
    for model in result.models:
        tier = f" [{model.access_tier}]" if model.access_tier else ""
        name = f" - {model.display_name}" if model.display_name else ""
        typer.echo(f"  {model.id}{name}{tier}")

    # GET /models does not check the key, so confirm it with a 1-token request.
    try:
        key_check = verify_key(result.models, key=key)
    except KeyRejected:
        _fail(_rejected_key())
    except ApiError as exc:
        _fail(str(exc))
    tokens = f", {key_check.total_tokens} tokens" if key_check.total_tokens is not None else ""
    typer.echo(f"OK: API key accepted. 1-token test on {key_check.model} took "
               f"{key_check.elapsed_ms / 1000:.1f} s{tokens}.")
    typer.echo(f"x-request-id: {key_check.request_id or 'not sent by /chat/completions'}")


@app.command()
def setkey(
    remove: bool = typer.Option(False, "--remove", help="Delete the saved key."),
    check_key: bool = typer.Option(True, "--check/--no-check",
                                   help="Test the key with one 1-token request before saving it."),
) -> None:
    """Save your BharatCode API key for your Windows account, so every terminal finds it."""
    if not keystore.supported():
        _fail(f"gtu setkey needs Windows. Set the {API_KEY_ENV} environment variable instead.")
    env_note = (f"Note: {API_KEY_ENV} is also set in this terminal's environment, and gtu uses "
                "that first." if find_api_key_in_env() else None)
    if remove:
        try:
            removed = keystore.delete()
        except keystore.KeystoreError as exc:
            _fail(str(exc))
        typer.echo(f"Removed the saved key from {keystore.LOCATION}." if removed
                   else "There was no saved key to remove.")
        if env_note:
            typer.echo(env_note)
        return
    _read_check_save_key(check_key)
    if env_note:
        typer.echo(env_note)


def _read_check_save_key(check_key: bool) -> None:
    """Ask for the key (or read it from a pipe), test it, save it in Credential Manager."""
    if sys.stdin is not None and not sys.stdin.isatty():
        # Piped in (`Get-Content key.txt | gtu setkey`). Hidden prompts on Windows read the
        # console directly and ignore a pipe, so they would wait forever. Read bytes: the
        # pipe's text encoding is a guess, and PowerShell may prefix a byte-order mark.
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.readline().decode("utf-8-sig", errors="replace") if stream else sys.stdin.readline()
    else:
        typer.echo(f"Get a key at {GET_KEY_URL} if you don't have one.")
        raw = typer.prompt("Paste your BharatCode API key (it won't be shown)", hide_input=True,
                           default="", show_default=False)
    key = clean_key(raw)
    if not key:
        _fail("no key entered; nothing saved.")
    problem = key_problem(key)
    if problem:
        _fail(f"that doesn't look like a key ({problem}); nothing saved.")
    if check_key:
        typer.echo("Checking it with BharatCode (one 1-token request; can take up to a minute)...")
        try:
            verify_key([], key=key)
        except KeyRejected:
            _fail(f"BharatCode rejected that key; nothing saved. Copy it again from {GET_KEY_URL}.")
        except ApiError as exc:
            typer.echo(f"Couldn't check it right now ({exc}). Saving it anyway; run `gtu check` later.")
        else:
            typer.echo("BharatCode accepted the key.")
    try:
        keystore.save(key)
    except keystore.KeystoreError as exc:
        _fail(f"{exc}; nothing saved.")
    typer.echo(f'Saved in {keystore.LOCATION} as "{keystore.target()}".')
    typer.echo("Only your Windows account can read it. It stays after you close the terminal or restart,")
    typer.echo("and gtu finds it in every terminal from now on. Nothing else on your system was changed.")
    typer.echo("Replace it: gtu setkey    Remove it: gtu setkey --remove")


def find_api_key_in_env() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


class JobKind(str, Enum):
    tests = "tests"
    docs = "docs"
    review = "review"
    fix = "fix"


def _fail(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def add(
    kind: JobKind = typer.Argument(..., help="What to do: tests, docs or review."),
    target: Path = typer.Argument(..., help="File or folder in this repo to work on."),
    note: str = typer.Option(None, "--note", help="Extra instructions for the job."),
) -> None:
    """Queue a job. It runs later with `gtu start`; its edited files are copied to spare-gpu/<date>_<time>/."""
    try:
        outcome = actions.queue_job(lambda repo: Store.open_default(), Path.cwd(), kind.value, str(target), note)
    except actions.ActionError as exc:
        _fail(str(exc))
    typer.echo(f"Queued job {outcome.job.id}: {outcome.job.kind} {outcome.job.target}")
    if outcome.note:
        typer.echo(f"Note: {outcome.note}")


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
    except ApiKeyMissing as exc:
        _fail(_no_key(exc))
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
    typer.echo("The demo sends one request now. This usually takes under a minute, but can take two or "
               "more when BharatCode is busy.\n")
    with Store.open_default() as store:
        client = BharatCodeClient(store=store, timeout=DEMO_TIMEOUT, retry=DEMO_RETRY,
                                  on_retry=lambda message: typer.echo(message))
        try:
            outcome = run_demo(root, target, client)
        except AuthError:
            _fail(_rejected_key())
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
        "That was one request. Real jobs run unattended, each on its own copy of the project:\n\n"
        f"  gtu add review {rel}   # a full review, saved as gtu-review-<id>.md\n"
        f"  gtu add tests {rel}    # tests for it; only ones that pass are kept\n"
        f"  gtu add docs {rel}     # docstrings and README additions\n"
        "  gtu start                # runs them\n"
        "  gtu list                 # what's queued, running and done"
    )


@app.command()
def start(
    max_steps: int = typer.Option(None, "--max-steps", help="Stop after this many completed steps."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show which jobs and steps would run and roughly what they'd cost; send nothing."
    ),
) -> None:
    """Run the queued jobs now, one step at a time, printing each step as it happens."""

    def log(message: str) -> None:
        typer.echo(f"[{datetime.now():%H:%M:%S}] {message}")

    if dry_run:
        try:
            job_config = JobConfig.from_env()
        except ValueError as exc:
            _fail(str(exc))
        with Store.open_default() as store:
            typer.echo(dryrun.describe(store, Budgets.from_env(), job_config, datetime.now(timezone.utc)))
        return

    with Store.open_default() as store:
        folder = store.path.parent
        other = runstate.running_pid(folder)
        if other:
            _fail(f"`gtu start` is already running (process {other}). Use `gtu stop` to stop it first.")
        try:
            job_config = JobConfig.from_env()
        except ValueError as exc:
            _fail(str(exc))
        try:
            worker = workers.build(store, log, job_config)
        except ApiKeyMissing as exc:
            _fail(_no_key(exc))
        client, agent, desktop_missing = worker.client, worker.agent, worker.desktop_missing
        runstate.clear(folder)  # a stop request left over from an earlier run doesn't apply to this one
        runstate.write_run(folder)
        def route(job):
            # A job delegated from a chat already has its model; a queued one is routed here, just
            # before it starts, from the worker's own model (or GTU_PREFERRED_MODEL).
            preferred = job.preferred_model or router.preferred_model(agent.model)
            return router.decide(store, folder, job.kind, preferred, lambda: httpx.Client(timeout=10.0))

        try:
            scheduler = Scheduler(
                store, client, agent=agent,
                budgets=Budgets.from_env(), config=job_config, log=log,
                should_stop=lambda: runstate.stop_requested(folder), desktop_missing=desktop_missing, route=route,
            )
            steps = scheduler.run(max_steps=max_steps)
        except AuthError:
            _fail(_rejected_key())
        except KeyboardInterrupt:
            paused = runstate.tidy(store)
            typer.echo("\nStopped." + (f" Paused job(s) {', '.join(paused)}." if paused else "")
                       + " Progress is saved; run `gtu start` again to resume.")
            raise typer.Exit(130)
        finally:
            runstate.clear(folder)
            if client is not None:
                client.close()
    if scheduler.stopped:
        return
    log(f"Completed {steps} step(s).")


@app.command()
def stop(
    wait: float = typer.Option(10.0, "--wait", min=0, help="Seconds to wait for the clean stop."),
) -> None:
    """Stop the running job cleanly (nothing is killed). Jobs are paused, not failed; `gtu start` resumes them."""
    with Store.open_default() as store:
        try:
            lines = actions.stop_runner(store, wait)
        except actions.ActionError as exc:
            _fail(str(exc))
    for line in lines:
        typer.echo(line)
    if lines[0].startswith("Nothing is running"):
        return
    typer.echo("Run `gtu start` to resume.")


@app.command()
def report(
    since: float = typer.Option(24.0, "--since", help="Hours to look back (default 24)."),
    job: str = typer.Option(None, "--job", help="Report on one job only."),
    all_jobs: bool = typer.Option(False, "--all", help="Every job ever run."),
) -> None:
    """Summary: what ran, tokens, what it produced, and the queue waits it met."""
    now = datetime.now(timezone.utc)
    start_from = None if all_jobs or job else now - timedelta(hours=since)
    with Store.open_default() as store:
        try:
            reports = [reporting.build_job(store, j) for j in reporting.select_jobs(store, start_from, job)]
        except StoreError as exc:
            _fail(str(exc))
        text = reporting.summary_markdown(reports, store, now, start_from)
        out = store.path.parent / "reports" / f"report-{now.astimezone(reporting.IST):%Y%m%d-%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(text)
    typer.echo(f"Saved to {out}")


def waits_cmd() -> None:
    """How long work would wait on each BharatCode model right now, and which would start sooner."""
    from .mcp_server import SpareTools

    typer.echo(SpareTools(Path.cwd()).waits())


app.command("waits")(waits_cmd)
app.command("advice", hidden=True)(waits_cmd)  # the older name


@app.command()
def mcp(
    folder: Path = typer.Option(None, "--dir", help="Project folder to serve. Default: the current folder."),
    with_pack: bool = typer.Option(False, "--pack", help="Also serve the command pack's tools (set by `gtu integrate --pack`)."),
) -> None:
    """Run spare-gpu's MCP server over stdio, so an agent in BharatCode Desktop can queue and check
    jobs. Started by the agent, not by hand; see the README for the config snippet."""
    from .mcp_server import serve

    serve(folder or Path.cwd(), pack=with_pack)


@app.command()
def doctor(
    channel: str = typer.Option("stable", "--channel", help="Which BharatCode's config to check: stable, beta, dev or local."),
    live: bool = typer.Option(False, "--live", help="Also ask BharatCode's CLI what Desktop really sees and run one tiny "
                                                   "request (one short model call) to measure the per-step context."),
    no_write: bool = typer.Option(False, "--no-write", help="With --live: don't write results into research/*/manual-check.md."),
) -> None:
    """Check the whole install and say what to fix: spare-gpu, git, BharatCode's CLI and config, the MCP
    entry (and that the server really starts), the pack, this project's jobs and settings. Exit code 1 on a FAIL."""
    from . import doctor as doctoring

    if channel not in bc_config.CHANNELS:
        _fail(f"unknown channel {channel!r}; use one of {', '.join(bc_config.CHANNELS)}.")
    report = doctoring.run(Path.cwd(), channel=channel, live_mode=live, write=not no_write)
    for finding in report.findings:
        typer.echo(str(finding))
    typer.echo(f"Result: {report.verdict}")
    if live:
        typer.echo(f"Still manual: {doctoring.MANUAL_LEFT}.")
    else:
        typer.echo("Add --live to check what Desktop really sees (needs BharatCode's CLI; one short model call).")
    if report.verdict == doctoring.FAIL:
        raise typer.Exit(1)


@app.command("pack-check")
def pack_check(
    channel: str = typer.Option("stable", "--channel", help="Which BharatCode's config to check: stable, beta, dev or local."),
) -> None:
    """Check the pack's install without starting BharatCode: the MCP entry, the settings file, every
    command and agent file, the model ids, helpers and tools hidden, and conflicts with your own
    settings. Exit code 1 on a FAIL. The checks that need Desktop stay in research/*/manual-check.md."""
    from .pack import check

    if channel not in bc_config.CHANNELS:
        _fail(f"unknown channel {channel!r}; use one of {', '.join(bc_config.CHANNELS)}.")
    folder, how = _channel_folder(channel)
    findings = check.run(folder, how) + check.check_channels()
    for finding in findings:
        typer.echo(str(finding))
    verdict = check.worst(findings)
    typer.echo(f"Result: {verdict}" + ("" if verdict == "PASS" else "; the checks that need Desktop are in research/*/manual-check.md"))
    if verdict == "FAIL":
        raise typer.Exit(1)


@app.command()
def cancel(job_id: str = typer.Argument(..., help="Job id from `gtu list`.")) -> None:
    """Cancel a job that has not finished."""
    with Store.open_default() as store:
        try:
            outcome = actions.cancel_job(store, job_id)
        except actions.ActionError as exc:
            _fail(str(exc))
    if outcome.note:
        typer.echo(f"warning: {outcome.note}", err=True)
    job = outcome.job
    typer.echo(f"Cancelled job {job.id}. Nothing was copied to {worktree.OUTPUT_DIR}/.")
