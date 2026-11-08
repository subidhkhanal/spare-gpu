"""spare-gpu's one command you type: `gtu integrate`, which installs spare-gpu into BharatCode
Desktop. Everything else is done from a Desktop chat. `gtu mcp` is what Desktop starts behind the
scenes (it's hidden from the help)."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import __version__
from . import bc_config, pack, waits


def _utf8_when_redirected() -> None:
    """On Windows, output sent to a file or pipe uses the ANSI code page (cp1252), which
    can't encode the check marks and emoji models like to write: `gtu integrate > out.txt`
    would crash with UnicodeEncodeError. Consoles are fine (Python writes Unicode to them
    directly), so only redirected streams switch to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # a replaced stream (tests, embedding) that can't be reconfigured


_utf8_when_redirected()


app = typer.Typer(
    help="Install spare-gpu into BharatCode Desktop: `gtu integrate --pack`, then restart Desktop. "
         "Everything else is done from a Desktop chat.",
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


def _fail(message: str) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


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
                   f"{path.name}{bc_config.BACKUP_SUFFIX}. Nothing else was changed; your jobs are kept.")
    else:
        typer.echo(f"spare-gpu wasn't registered in {path}; nothing changed.")


@app.command(hidden=True)
def mcp(
    folder: Path = typer.Option(None, "--dir", help="Project folder to serve. Default: the current folder."),
    with_pack: bool = typer.Option(False, "--pack", help="Also serve the command pack's tools (set by `gtu integrate --pack`)."),
) -> None:
    """Run spare-gpu's MCP server over stdio. BharatCode Desktop starts it (`gtu integrate` registers
    it); it isn't run by hand."""
    from .mcp_server import serve

    serve(folder or Path.cwd(), pack=with_pack)
