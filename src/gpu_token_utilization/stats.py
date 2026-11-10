"""Tokens per model from BharatCode Desktop's own session database: the figures behind the pack's
pack_cost tool and the benchmarks. Desktop records every reply with its model and exact token
counts (input, output, reasoning, cache read and write), so these figures are exact, and they can
be limited to one project's sessions.

The database is only ever read: opened read-only, never migrated or written."""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

from . import tuning
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


def session_db(env: dict[str, str] | None = None, which: str | None = None) -> Path | None:
    """A channel's session database, if it exists: `which` (stable, beta, dev or local), else the
    channel that started us (GTU_DESKTOP_DB names another file instead)."""
    env = os.environ if env is None else env
    if env.get(DB_ENV):
        path = Path(env[DB_ENV])
    else:
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / CHANNELS[which or channel(env)] / "Data" / "bharatcode.db"
    return path if path.is_file() else None


def _same_folder(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a.replace("/", os.sep))) == os.path.normcase(os.path.normpath(str(b)))


START_WINDOW = tuning.value("router", "start_window", 30.0)  # minutes of replies start_times() looks at
START_KEEP = tuning.value("router", "start_keep", 5)  # the latest replies per model it uses
START_MIN = tuning.value("router", "start_min", 2)  # replies a model needs before its figure counts


def start_times(minutes: float = START_WINDOW, db: Path | None = None, now: float | None = None) -> dict[str, float]:
    """How long each model took to start answering in this user's own Desktop chats lately: per
    model id, the median seconds from a reply's creation to its first output (text, reasoning or
    a tool call), over its latest START_KEEP replies in the last `minutes`. Replies that failed
    are left out, and so is a model with fewer than START_MIN replies. {} without a database.
    This is what the user actually waited, which BharatCode's queue figures don't always show."""
    since = (now if now is not None else datetime.now().timestamp()) - minutes * 60
    return summarise_starts(reply_starts(since, db))


def summarise_starts(replies: list[tuple[str, float, float]], keep: int | None = None,
                     minimum: int | None = None) -> dict[str, float]:
    """Per model, the median start of its latest `keep` replies, for a model with `minimum` or more.
    replies: (model id, created in epoch seconds, seconds to its first output)."""
    keep = START_KEEP if keep is None else keep
    minimum = START_MIN if minimum is None else minimum
    per_model: dict[str, list[tuple[float, float]]] = {}
    for model, created, seconds in replies:
        per_model.setdefault(model, []).append((created, seconds))
    figures = {}
    for model, seen in per_model.items():
        latest = [seconds for _, seconds in sorted(seen)[-keep:]] if keep > 0 else []
        if latest and len(latest) >= minimum:
            figures[model] = statistics.median(latest)
    return figures


def reply_starts(since: float, db: Path | None = None) -> list[tuple[str, float, float]]:
    """Every reply Desktop recorded since `since` (epoch seconds) that produced output: (model id,
    created in epoch seconds, seconds to its first output), oldest first. Failed replies are left
    out. [] without a database."""
    path = db or session_db()
    if path is None:
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("select m.id, m.data, p.data from message m join part p on p.message_id = m.id "
                            "where m.time_created >= ?", (int(since * 1000),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    replies: dict[str, tuple[str, float, float]] = {}  # message id -> (model, created, first output)
    for mid, mdata, pdata in rows:
        try:
            message, part = json.loads(mdata), json.loads(pdata)
        except ValueError:
            continue
        created = (message.get("time") or {}).get("created")
        if message.get("role") != "assistant" or message.get("error") or not created or not message.get("modelID"):
            continue
        if part.get("type") not in ("text", "reasoning", "tool"):
            continue
        start = (part.get("time") or (part.get("state") or {}).get("time") or {}).get("start")
        if not start:
            continue
        known = replies.get(mid)
        if known is None or start < known[2]:
            replies[mid] = (message["modelID"], created, start)
    return sorted(((model, created / 1000, max(0.0, (start - created) / 1000))
                   for model, created, start in replies.values()), key=lambda r: r[1])


# A helper's model: the one its session was started with (so a helper still queued counts too),
# else its first message's, for databases that don't keep the session's model.
_FIRST_MESSAGE_MODEL = """(select coalesce(json_extract(m.data, '$.model.providerID'), json_extract(m.data, '$.providerID'))
        || '/' || coalesce(json_extract(m.data, '$.model.modelID'), json_extract(m.data, '$.modelID'))
    from message m where m.session_id = s.id
      and coalesce(json_extract(m.data, '$.model.modelID'), json_extract(m.data, '$.modelID')) is not null
    order by m.time_created limit 1)"""
_HELPER_MODEL = f"coalesce(json_extract(s.model, '$.providerID') || '/' || json_extract(s.model, '$.id'), {_FIRST_MESSAGE_MODEL})"


def _helpers(conn: sqlite3.Connection, started: int, directory: Path | None, caller_tool: str | None) -> dict[str, int]:
    """Helper sessions started since `started`, per model, counted from the moment each was started
    (queued and failed ones too). With `caller_tool`, only the helpers of the session that is running
    that tool right now (the lead calling it), wherever they work; else every helper in `directory`."""
    lead = None
    if caller_tool:
        for sid, folder in conn.execute(
                "select p.session_id, s.directory from part p join session s on s.id = p.session_id "
                "where p.time_created >= ? and json_extract(p.data, '$.tool') = ? "
                "and json_extract(p.data, '$.state.status') in ('pending', 'running') order by p.time_created desc",
                (started, caller_tool)):
            if directory is None or (folder and _same_folder(folder, Path(directory))):
                lead = sid
                break
    columns = {row[1] for row in conn.execute("pragma table_info(session)")}
    model_sql = _HELPER_MODEL if "model" in columns else _FIRST_MESSAGE_MODEL
    if lead is not None:
        rows = conn.execute(f"select s.directory, {model_sql} from session s where s.parent_id = ? and s.time_created >= ?",
                            (lead, started)).fetchall()
    else:
        rows = [(folder, model) for folder, model in conn.execute(
            f"select s.directory, {model_sql} from session s where s.parent_id is not null and s.time_created >= ?",
            (started,)) if directory is None or (folder and _same_folder(folder, Path(directory)))]
    runs: dict[str, int] = {}
    for _, model in rows:
        runs[model or "unknown"] = runs.get(model or "unknown", 0) + 1
    return runs


def helper_runs(since: datetime, directory: Path | None = None, db: Path | None = None,
                caller_tool: str | None = None) -> dict[str, int] | None:
    """How many helper sessions (sessions started by another session, such as a task's subagent)
    began since `since`, per model. None when there's no database, or it doesn't record which
    session started which."""
    return usage_since(since, directory, db, caller_tool)[1]


def usage_since(since: datetime, directory: Path | None = None, db: Path | None = None,
                caller_tool: str | None = None) -> tuple[dict[str, dict] | None, dict[str, int] | None]:
    """tokens_since and helper_runs from one read of the database."""
    path = db or session_db()
    if path is None:
        return None, None
    started = int(since.timestamp() * 1000)
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, None
    try:
        try:
            tokens = _tokens(conn, started, directory)
        except sqlite3.Error:
            tokens = None
        try:
            runs = _helpers(conn, started, directory, caller_tool)
        except sqlite3.Error:
            runs = None
    finally:
        conn.close()
    return tokens, runs


def tokens_since(since: datetime, directory: Path | None = None, db: Path | None = None) -> dict[str, dict] | None:
    """Tokens per model ("bharatcode/qwen-3.8-27b": {messages, input, output, reasoning,
    cache_read, cache_write, tokens}) in the replies Desktop recorded since `since`, in the
    sessions of `directory` only when it's given. None when there's no database to read."""
    return usage_since(since, directory, db)[0]


def _tokens(conn: sqlite3.Connection, started: int, directory: Path | None) -> dict[str, dict]:
    rows = conn.execute("select m.data, s.directory from message m join session s on s.id = m.session_id "
                        "where m.time_created >= ?", (started,)).fetchall()
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
