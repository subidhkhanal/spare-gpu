"""Tokens per model from BharatCode Desktop's own session database: the figures behind the pack's
pack_cost tool and the benchmarks. Desktop records every reply with its model and exact token
counts (input, output, reasoning, cache read and write), so these figures are exact, and they can
be limited to one project's sessions.

The database is only ever read: opened read-only, never migrated or written."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .bc_config import CHANNELS

DB_ENV = "GTU_DESKTOP_DB"  # a database file to read instead of the channel's (tests, or a copy)
DESKTOP_STATE = "ai.bharatcode.desktop"  # Desktop's XDG_STATE_HOME folder, with ".beta"/".dev"/".local" per channel
FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")


def channel(env: dict[str, str] | None = None) -> str:
    """The Desktop channel that started us (Desktop passes XDG_STATE_HOME, …\\ai.bharatcode.desktop.beta
    for Beta, to the MCP servers it starts); the released app's when we weren't started by one."""
    env = os.environ if env is None else env
    name = Path(env.get("XDG_STATE_HOME", "")).name.lower()
    if name.startswith(DESKTOP_STATE):
        found = name[len(DESKTOP_STATE):].lstrip(".") or "stable"
        if found in CHANNELS:
            return found
    return "stable"


def session_db(env: dict[str, str] | None = None) -> Path | None:
    """That channel's session database, if it exists (GTU_DESKTOP_DB names another file)."""
    env = os.environ if env is None else env
    if env.get(DB_ENV):
        path = Path(env[DB_ENV])
    else:
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / CHANNELS[channel(env)] / "Data" / "bharatcode.db"
    return path if path.is_file() else None


def _same_folder(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a.replace("/", os.sep))) == os.path.normcase(os.path.normpath(str(b)))


def tokens_since(since: datetime, directory: Path | None = None, db: Path | None = None) -> dict[str, dict] | None:
    """Tokens per model ("bharatcode/qwen-3.8-27b": {messages, input, output, reasoning,
    cache_read, cache_write, tokens}) in the replies Desktop recorded since `since`, in the
    sessions of `directory` only when it's given. None when there's no database to read."""
    path = db or session_db()
    if path is None:
        return None
    started = int(since.timestamp() * 1000)
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute("select m.data, s.directory from message m join session s on s.id = m.session_id "
                            "where m.time_created >= ?", (started,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    models: dict[str, dict] = {}
    for data, folder in rows:
        if directory is not None and not (folder and _same_folder(folder, Path(directory))):
            continue
        try:
            message = json.loads(data)
        except ValueError:
            continue
        tokens = message.get("tokens") if message.get("role") == "assistant" else None
        if not tokens:
            continue
        model = f"{message.get('providerID') or 'bharatcode'}/{message.get('modelID') or 'unknown'}"
        cache = tokens.get("cache") or {}
        figures = models.setdefault(model, {"messages": 0, **{f: 0 for f in FIELDS}, "tokens": 0})
        values = {"input": tokens.get("input"), "output": tokens.get("output"), "reasoning": tokens.get("reasoning"),
                  "cache_read": cache.get("read"), "cache_write": cache.get("write")}
        for name, value in values.items():
            figures[name] += value or 0
        figures["messages"] += 1
        figures["tokens"] += tokens.get("total") or sum(v or 0 for v in values.values())
    return models
