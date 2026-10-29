"""SQLite job store: jobs, job steps and the request log."""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

JOB_KINDS = ("tests", "docs", "review")
JOB_STATUSES = ("queued", "running", "paused", "done", "failed", "cancelled")
FINAL_STATUSES = ("done", "failed", "cancelled")
STEP_STATUSES = ("pending", "running", "done", "failed")

DB_ENV = "GTU_DB"
DEFAULT_DB = Path(".gtu") / "gtu.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('tests', 'docs', 'review')),
    target      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')),
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    seq          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed')),
    output       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    UNIQUE (job_id, seq)
);

CREATE TABLE IF NOT EXISTS requests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT REFERENCES jobs(id),
    step_id            INTEGER REFERENCES job_steps(id),
    sent_at            TEXT NOT NULL,
    endpoint           TEXT NOT NULL,
    model              TEXT,
    attempt            INTEGER NOT NULL,
    status_code        INTEGER,
    elapsed_ms         REAL,
    request_id         TEXT,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens       INTEGER NOT NULL DEFAULT 0,
    finish_reason      TEXT,
    truncated          INTEGER NOT NULL DEFAULT 0,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS gate_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at      TEXT NOT NULL,
    source        TEXT NOT NULL,
    model         TEXT,
    active        INTEGER,
    queued        INTEGER,
    slots         INTEGER,
    verdict       TEXT NOT NULL CHECK (verdict IN ('quiet', 'busy', 'unknown')),
    decision      TEXT NOT NULL CHECK (decision IN ('open', 'closed')),
    quiet_streak  INTEGER NOT NULL,
    busy_streak   INTEGER NOT NULL,
    reason        TEXT NOT NULL
);
"""

# Columns added after Phase 1; older databases get them on open.
ADDED_COLUMNS = {
    "requests": {"gate_source": "TEXT", "queue_at_send": "INTEGER", "active_at_send": "INTEGER"},
}


class StoreError(Exception):
    """A job operation the store refuses (unknown job, wrong state)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    target: str
    repo: str
    status: str
    note: str | None
    created_at: str
    updated_at: str


@dataclass
class RequestLog:
    endpoint: str
    attempt: int
    model: str | None = None
    status_code: int | None = None
    elapsed_ms: float | None = None
    request_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None
    truncated: bool = False
    error: str | None = None
    job_id: str | None = None
    step_id: int | None = None
    gate_source: str | None = None
    queue_at_send: int | None = None
    active_at_send: int | None = None


@dataclass
class GateReading:
    taken_at: str
    source: str
    verdict: str
    decision: str
    quiet_streak: int
    busy_streak: int
    reason: str
    model: str | None = None
    active: int | None = None
    queued: int | None = None
    slots: int | None = None
    id: int | None = None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the store out of whatever repo it is created in.
        marker = self.path.parent / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf-8")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.conn.commit()

    @classmethod
    def open_default(cls) -> Store:
        return cls(os.environ.get(DB_ENV) or DEFAULT_DB)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # jobs

    def add_job(self, kind: str, target: str, repo: str, note: str | None = None) -> Job:
        if kind not in JOB_KINDS:
            raise StoreError(f"unknown job kind {kind!r}; expected one of {', '.join(JOB_KINDS)}")
        job_id = secrets.token_hex(4)
        at = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO jobs (id, kind, target, repo, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, kind, target, repo, note, at, at),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise StoreError(f"no job with id {job_id!r}")
        return Job(**dict(row))

    def list_jobs(self, include_finished: bool = True) -> list[Job]:
        if include_finished:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()
        else:
            marks = ", ".join("?" for _ in FINAL_STATUSES)
            rows = self.conn.execute(
                f"SELECT * FROM jobs WHERE status NOT IN ({marks}) ORDER BY created_at, rowid", FINAL_STATUSES
            ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def set_status(self, job_id: str, status: str) -> Job:
        if status not in JOB_STATUSES:
            raise StoreError(f"unknown job status {status!r}")
        self.get_job(job_id)
        with self.conn:
            self.conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), job_id))
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.status in FINAL_STATUSES:
            raise StoreError(f"job {job_id} is already {job.status}")
        return self.set_status(job_id, "cancelled")

    # steps

    def add_step(self, job_id: str, name: str) -> int:
        self.get_job(job_id)
        with self.conn:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_steps WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            cur = self.conn.execute("INSERT INTO job_steps (job_id, seq, name) VALUES (?, ?, ?)", (job_id, seq, name))
        return cur.lastrowid

    def set_step(self, step_id: int, status: str, output: str | None = None) -> None:
        if status not in STEP_STATUSES:
            raise StoreError(f"unknown step status {status!r}")
        at = utc_now()
        with self.conn:
            if status == "running":
                self.conn.execute("UPDATE job_steps SET status = ?, started_at = ? WHERE id = ?", (status, at, step_id))
            else:
                self.conn.execute(
                    "UPDATE job_steps SET status = ?, output = COALESCE(?, output), finished_at = ? WHERE id = ?",
                    (status, output, at, step_id),
                )

    def steps(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM job_steps WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()

    def reset_interrupted_steps(self, job_id: str) -> int:
        """Steps left 'running' by a killed process never finished; make them pending again."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE job_steps SET status = 'pending', started_at = NULL WHERE job_id = ? AND status = 'running'",
                (job_id,),
            )
        return cur.rowcount

    # request log

    def log_request(self, entry: RequestLog) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO requests (job_id, step_id, sent_at, endpoint, model, attempt, status_code,
                       elapsed_ms, request_id, prompt_tokens, completion_tokens, reasoning_tokens,
                       total_tokens, finish_reason, truncated, error, gate_source, queue_at_send, active_at_send)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.job_id, entry.step_id, utc_now(), entry.endpoint, entry.model, entry.attempt,
                    entry.status_code, entry.elapsed_ms, entry.request_id, entry.prompt_tokens,
                    entry.completion_tokens, entry.reasoning_tokens, entry.total_tokens,
                    entry.finish_reason, int(entry.truncated), entry.error,
                    entry.gate_source, entry.queue_at_send, entry.active_at_send,
                ),
            )
        return cur.lastrowid

    def requests(self, job_id: str | None = None) -> list[sqlite3.Row]:
        if job_id is None:
            return self.conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM requests WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()

    def recent_requests(self, limit: int) -> list[sqlite3.Row]:
        """Most recent request attempts, newest first."""
        return self.conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def recent_latencies(self, limit: int) -> list[float]:
        """Response times (ms) of the most recent successful requests."""
        rows = self.conn.execute(
            "SELECT elapsed_ms FROM requests WHERE status_code = 200 AND elapsed_ms IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    # gate readings

    def add_gate_reading(self, reading: GateReading) -> GateReading:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO gate_readings (taken_at, source, model, active, queued, slots, verdict, decision,
                       quiet_streak, busy_streak, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reading.taken_at, reading.source, reading.model, reading.active, reading.queued, reading.slots,
                    reading.verdict, reading.decision, reading.quiet_streak, reading.busy_streak, reading.reason,
                ),
            )
        reading.id = cur.lastrowid
        return reading

    def last_gate_reading(self) -> GateReading | None:
        row = self.conn.execute("SELECT * FROM gate_readings ORDER BY id DESC LIMIT 1").fetchone()
        return GateReading(**dict(row)) if row else None

    def request_summary(self, job_id: str | None = None) -> dict:
        where, args = ("WHERE job_id = ?", (job_id,)) if job_id is not None else ("", ())
        row = self.conn.execute(
            f"""SELECT COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(truncated), 0) AS truncated,
                       COALESCE(SUM(status_code IN (429, 503)), 0) AS busy,
                       (SELECT request_id FROM requests {where} ORDER BY id DESC LIMIT 1) AS last_request_id
                FROM requests {where}""",
            args * 2,
        ).fetchone()
        return dict(row)
