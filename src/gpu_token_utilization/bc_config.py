"""Registering spare-gpu's MCP server in BharatCode's CLI config (`gtu setup`).

BharatCode (built on OpenCode) reads a global JSONC file, e.g.
%APPDATA%\\BharatCode\\Config\\opencode.jsonc. We add an "mcp" entry for spare-gpu:
- as a text insert when we can, so the user's comments and layout survive;
- as a rewrite (after a backup) only when replacing an outdated spare-gpu entry;
- never leaving behind a file that doesn't parse.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .agent_cli import CliNotFound, resolve_command
from .api import env_without_key

SERVER_NAME = "spare-gpu"
CONFIG_DIR_ENV = "GTU_BHARATCODE_CONFIG_DIR"  # tests (and unusual setups) point this elsewhere
CONFIG_NAMES = ("opencode.jsonc", "opencode.json", "config.json")  # BharatCode ignores bharatcode.json
BACKUP_SUFFIX = ".spare-gpu-backup"


class ConfigError(Exception):
    """The config file couldn't be read or safely changed; the message says what to do."""


def locate_config_dir() -> tuple[Path, str]:
    """Where BharatCode keeps its global config, and how we know: asked from its CLI when it's
    installed, else BharatCode's default location."""
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override), f"from {CONFIG_DIR_ENV}"
    try:
        proc = subprocess.run([*resolve_command(), "debug", "paths"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                              env=env_without_key())
        for line in (proc.stdout + proc.stderr).splitlines():
            parts = re.sub(r"\x1b\[[0-9;]*m", "", line).split(None, 1)
            if len(parts) == 2 and parts[0] == "config":
                return Path(parts[1].strip()), "reported by `bharatcode debug paths`"
    except (CliNotFound, OSError, subprocess.TimeoutExpired):
        pass
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "BharatCode" / "Config", "BharatCode's default location (its CLI didn't say)"


def config_dir() -> Path:
    return locate_config_dir()[0]


# BharatCode keeps one config folder per release channel, named after the product (core's
# storage-paths.ts): the CLI and the released Desktop app share "BharatCode"; Desktop's beta,
# dev and local builds use their own. A Desktop build from source runs as "dev" by default.
CHANNELS = {"stable": "BharatCode", "beta": "BharatCode Beta", "dev": "BharatCode Dev", "local": "BharatCode Local"}


def channel_config_dir(channel: str, ask_cli: bool = True) -> Path:
    """A channel's config folder. The stable one is what BharatCode's CLI reports (unless
    `ask_cli` is off: starting the CLI is slow and not for a job's start), else the default."""
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:  # tests: the other channels' folders sit under the override
        return Path(override) if channel == "stable" else Path(override) / CHANNELS[channel]
    if channel == "stable" and ask_cli:
        return config_dir()
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / CHANNELS[channel] / "Config"


def installed_channels() -> list[str]:
    """Channels with a config folder on this machine; stable always (the CLI and released app)."""
    return ["stable"] + [c for c in CHANNELS if c != "stable" and channel_config_dir(c).is_dir()]


def user_mcp_servers() -> dict[str, dict]:
    """The MCP servers in the user's global BharatCode config, by name, from every channel's
    config folder on this machine (the released app and the CLI share one; beta and dev builds
    have their own; the stable one wins a clash). For jobs, which switch them off: the folders
    are found without starting the CLI, and a file that can't be read or parsed is skipped, so
    the user's config can never stop a job."""
    found: dict[str, dict] = {}
    for channel in CHANNELS:
        folder = channel_config_dir(channel, ask_cli=False)
        if not folder.is_dir():
            continue
        try:
            mcp = parse_jsonc(config_file(folder).read_text(encoding="utf-8-sig")).get("mcp")
        except (OSError, ValueError):
            continue
        if isinstance(mcp, dict):
            for name, entry in mcp.items():
                if isinstance(entry, dict):
                    found.setdefault(name, entry)
    return found


def snippet(command: list[str]) -> str:
    """What to paste by hand when the file can't be edited automatically."""
    return json.dumps({"mcp": {SERVER_NAME: server_entry(command)}}, indent=2)


def config_file(folder: Path) -> Path:
    for name in CONFIG_NAMES:
        if (folder / name).exists():
            return folder / name
    return folder / CONFIG_NAMES[0]


def server_command() -> list[str]:
    """How BharatCode should start our server: an absolute path, so it works even if BharatCode
    was opened before PATH was updated. The gtu running this setup comes first: whatever `gtu`
    is first on PATH could be an older install without the MCP server."""
    exe = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if exe and exe.suffix.lower() == ".exe" and exe.stem.lower() == "gtu" and exe.exists():
        return [str(exe.resolve()), "mcp"]
    found = shutil.which("gtu")
    if found and Path(found).resolve().parent == Path(sys.executable).resolve().parent:
        return [str(Path(found).resolve()), "mcp"]  # the gtu of this very install
    return [sys.executable, "-m", "gpu_token_utilization", "mcp"]


def server_entry(command: list[str]) -> dict:
    return {"type": "local", "command": command, "enabled": True}


# --- JSONC -------------------------------------------------------------------------------------

def parse_jsonc(text: str) -> dict:
    """JSON with // and /* */ comments and trailing commas, as BharatCode accepts."""
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    value = json.loads(cleaned) if cleaned.strip() else {}
    if not isinstance(value, dict):
        raise ValueError("the top level isn't an object")
    return value


def _entry_text(entry: dict, indent: str) -> str:
    body = json.dumps(entry, indent=2).replace("\n", "\n" + indent)
    return f'{indent}"{SERVER_NAME}": {body}'


def _insert_after(text: str, at: int, addition: str, object_was_empty: bool) -> str:
    return text[:at] + "\n" + addition + ("" if object_was_empty else ",") + text[at:]


def _object_empty_after(text: str, brace: int) -> bool:
    rest = re.sub(r"//[^\n]*|/\*.*?\*/", "", text[brace + 1:], flags=re.S).lstrip()
    return rest.startswith("}")


def register(path: Path, command: list[str]) -> str:
    """Add or update spare-gpu's entry. Returns "added", "updated" or "unchanged"."""
    entry = server_entry(command)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    try:
        config = parse_jsonc(original)
    except ValueError as exc:
        raise ConfigError(f"{path} isn't valid JSON ({exc}); fix it or add the entry by hand (see the README)") from None
    mcp = config.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        raise ConfigError(f'"mcp" in {path} isn\'t an object; add the entry by hand (see the README)')
    current = (mcp or {}).get(SERVER_NAME)
    if current == entry:
        return "unchanged"

    if not original.strip():
        new = json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {SERVER_NAME: entry}}, indent=2) + "\n"
    elif current is not None:
        config["mcp"][SERVER_NAME] = entry  # replacing ours: rewrite (comments are lost; backed up)
        new = json.dumps(config, indent=2) + "\n"
    elif mcp is None:
        top = original.index("{")
        block = '  "mcp": {\n' + _entry_text(entry, "    ") + "\n  }"
        new = _insert_after(original, top + 1, block, _object_empty_after(original, top))
    else:
        found = re.search(r'"mcp"\s*:\s*\{', original)
        if not found:
            raise ConfigError(f'couldn\'t find where "mcp" starts in {path}; add the entry by hand (see the README)')
        new = _insert_after(original, found.end(), _entry_text(entry, "    "), _object_empty_after(original, found.end() - 1))

    try:
        if parse_jsonc(new).get("mcp", {}).get(SERVER_NAME) != entry:
            raise ValueError("entry missing after the edit")
    except ValueError as exc:
        raise ConfigError(f"couldn't safely edit {path} ({exc}); nothing was changed. Add the entry by hand "
                          "(see the README).") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    if original.strip():
        path.with_name(path.name + BACKUP_SUFFIX).write_text(original, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    return "added" if current is None else "updated"


def unregister(path: Path) -> bool:
    """Remove spare-gpu's entry (backing the file up first). False if it wasn't there."""
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8-sig")
    try:
        config = parse_jsonc(original)
    except ValueError as exc:
        raise ConfigError(f"{path} isn't valid JSON ({exc}); remove the spare-gpu entry by hand") from None
    if SERVER_NAME not in (config.get("mcp") or {}):
        return False
    del config["mcp"][SERVER_NAME]
    if not config["mcp"]:
        del config["mcp"]
    path.with_name(path.name + BACKUP_SUFFIX).write_text(original, encoding="utf-8")
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return True


# Slash commands spare-gpu adds to BharatCode Desktop (it loads <config folder>/command/*.md).
COMMANDS_DIR = Path(__file__).with_name("commands")
COMMAND_MARKER = "# Installed by spare-gpu"


def _ours(path: Path) -> bool:
    try:
        return COMMAND_MARKER in path.read_text(encoding="utf-8-sig")
    except OSError:
        return False


def install_commands(folder: Path) -> dict[str, str]:
    """Copy spare-gpu's slash commands into <folder>/command/. A file of the same name the user
    wrote (or edited, removing the marker) is left alone. {name: added|updated|unchanged|kept-yours}."""
    target = folder / "command"
    outcome = {}
    for source in sorted(COMMANDS_DIR.glob("*.md")):
        dest, text = target / source.name, source.read_text(encoding="utf-8")
        if dest.exists() and not _ours(dest):
            outcome[source.stem] = "kept-yours"
        elif dest.exists() and dest.read_text(encoding="utf-8-sig") == text:
            outcome[source.stem] = "unchanged"
        else:
            existed = dest.exists()
            target.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            outcome[source.stem] = "updated" if existed else "added"
    return outcome


def remove_commands(folder: Path) -> list[str]:
    """Remove the slash commands spare-gpu installed (only files still carrying its marker)."""
    removed = []
    for source in sorted(COMMANDS_DIR.glob("*.md")):
        dest = folder / "command" / source.name
        if dest.exists() and _ours(dest):
            dest.unlink()
            removed.append(source.stem)
    return removed


def connected(path: Path, timeout: float = 90.0) -> bool | None:
    """Ask BharatCode's CLI whether spare-gpu connects (None if the CLI can't be run). Points it
    at exactly the file we edited."""
    try:
        args = [*resolve_command(), "mcp", "list"]
    except CliNotFound:
        return None
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, cwd=Path.home(),
                              env=env_without_key(OPENCODE_CONFIG=str(path)))
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr)
    return any(SERVER_NAME in line and "connected" in line and "not connected" not in line
               for line in text.splitlines())
