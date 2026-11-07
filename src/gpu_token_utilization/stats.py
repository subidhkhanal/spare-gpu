"""Reading `bharatcode stats --models`: the per-model token figures behind the pack's pack_cost
tool and the benchmark scripts. BharatCode prints the counts rounded to 0.1K (or 0.1M), so a
delta between two readings is good to about a hundred tokens per model."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .agent_cli import CliNotFound, resolve_command
from .api import env_without_key
from .bc_config import CHANNELS

DB_ENV = "OPENCODE_DB"  # the CLI reads its sessions from this database file when it is set
DESKTOP_STATE = "ai.bharatcode.desktop"  # Desktop's XDG_STATE_HOME folder, with ".beta"/".dev"/".local" per channel


def desktop_db(env: dict[str, str] | None = None) -> Path | None:
    """The session database of the Desktop channel that started us, when it isn't the one the CLI
    reads anyway. Desktop passes XDG_STATE_HOME (…\\ai.bharatcode.desktop.beta for Beta) to the MCP
    servers it starts; the CLI on its own reads the stable channel, which the released app shares."""
    env = os.environ if env is None else env
    if env.get(DB_ENV):
        return None  # chosen already; the CLI honours it
    name = Path(env.get("XDG_STATE_HOME", "")).name.lower()
    if not name.startswith(DESKTOP_STATE):
        return None
    channel = name[len(DESKTOP_STATE):].lstrip(".") or "stable"
    if channel == "stable" or channel not in CHANNELS:
        return None
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / CHANNELS[channel] / "Data" / "bharatcode.db"
    return path if path.is_file() else None


def _snapshot(source: Path, folder: Path) -> Path:
    """A consistent copy of a live database (SQLite's backup, the source opened read-only), so
    the CLI never opens, and never migrates, the one Desktop is using."""
    copy = folder / "bharatcode.db"
    src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return copy


def usage(days: int = 7, command: list[str] | None = None, timeout: float = 60.0) -> str | None:
    """`bharatcode stats --days N --models`, as text; None when the CLI isn't there or has no stats.
    Started by a Beta, Dev or Local Desktop, it reads a snapshot of that channel's sessions."""
    try:
        argv = command or resolve_command()
    except CliNotFound:
        return None
    with tempfile.TemporaryDirectory(prefix="gtu-stats-") as folder:
        extra = {"NO_COLOR": "1", "TERM": "dumb"}
        source = desktop_db()
        if source is not None:
            try:
                extra[DB_ENV] = str(_snapshot(source, Path(folder)))
            except sqlite3.Error:
                return None  # better no figures than the wrong channel's
        try:
            proc = subprocess.run([*argv, "stats", "--days", str(days), "--models"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env_without_key(**extra))
        except (OSError, subprocess.TimeoutExpired):
            return None
    if proc.returncode != 0:
        return None
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", proc.stdout).strip()  # every CSI sequence: colours, erase-line, cursor
    return text or None


MODEL_LINE = re.compile(r"[A-Za-z][\w.-]*/[\w.:+-]+")
STATS_ROW = re.compile(r"^\s*(Messages|Input Tokens|Output Tokens|Cache Read|Cache Write|Cost)\s+(\S+)\s*$")


def _stats_number(value: str) -> float:
    value = value.replace(",", "").lstrip("$")
    scale = {"K": 1_000, "M": 1_000_000}.get(value[-1:].upper(), 1)
    try:
        return float(value.rstrip("KkMm")) * scale
    except ValueError:
        return 0.0


def parse_stats(text: str) -> dict[str, dict[str, float]]:
    """The MODEL USAGE section of `bharatcode stats --models`, by model: messages, input, output,
    cache_read, cache_write and tokens (their sum). BharatCode prints the counts rounded to 0.1K
    (or 0.1M), so a delta between two readings is good to about a hundred tokens per model."""
    models: dict[str, dict[str, float]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip().strip("│").strip()
        if not line or set(line) <= set("─┌┐└┘├┤ "):
            continue
        if "TOOL USAGE" in line:
            break  # the tool box comes after the models; nothing in it is a model
        row = STATS_ROW.match(line)
        if row and current is not None:
            key = {"Messages": "messages", "Input Tokens": "input", "Output Tokens": "output",
                   "Cache Read": "cache_read", "Cache Write": "cache_write", "Cost": "cost"}[row.group(1)]
            models[current][key] = _stats_number(row.group(2))
        elif MODEL_LINE.fullmatch(line):  # provider/model, as the box prints it; not a date or a path
            current = line
            models[current] = {"messages": 0.0, "input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost": 0.0}
    for figures in models.values():
        figures["tokens"] = figures["input"] + figures["output"] + figures["cache_read"] + figures["cache_write"]
    return models


def model_usage(days: int = 1, command: list[str] | None = None) -> dict[str, dict[str, float]] | None:
    text = usage(days, command=command)
    return None if text is None else parse_stats(text)
