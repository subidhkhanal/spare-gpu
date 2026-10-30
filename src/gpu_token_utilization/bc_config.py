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


def config_dir() -> Path:
    """Where BharatCode keeps its global config: asked from the CLI when installed."""
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override)
    try:
        proc = subprocess.run([*resolve_command(), "debug", "paths"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                              env=env_without_key())
        for line in proc.stdout.splitlines():
            parts = re.sub(r"\x1b\[[0-9;]*m", "", line).split(None, 1)
            if len(parts) == 2 and parts[0] == "config":
                return Path(parts[1].strip())
    except (CliNotFound, OSError, subprocess.TimeoutExpired):
        pass
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "BharatCode" / "Config"


def config_file(folder: Path) -> Path:
    for name in CONFIG_NAMES:
        if (folder / name).exists():
            return folder / name
    return folder / CONFIG_NAMES[0]


def server_command() -> list[str]:
    """How BharatCode should start our server: an absolute path, so it works even if BharatCode
    was opened before PATH was updated. pipx's stable gtu.exe when there is one."""
    found = shutil.which("gtu")
    if found:
        return [str(Path(found).resolve()), "mcp"]
    exe = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if exe and exe.suffix.lower() == ".exe" and exe.stem.lower() == "gtu" and exe.exists():
        return [str(exe.resolve()), "mcp"]
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
