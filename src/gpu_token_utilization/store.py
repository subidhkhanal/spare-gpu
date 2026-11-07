"""SQLite job store: jobs, job steps and the request log."""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

JOB_KINDS = ("tests", "docs", "review", "fix")
JOB_STATUSES = ("queued", "running", "paused", "done", "failed", "cancelled")
FINAL_STATUSES = ("done", "failed", "cancelled")
STEP_STATUSES = ("pending", "running", "done", "failed")

DB_ENV = "GTU_DB"
DEFAULT_DB = Path(".gtu") / "gtu.db"
BUSY_TIMEOUT_S = 30.0

JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN (__KINDS__)),
    target      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')),
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)""".replace("__KINDS__", ", ".join(f"'{k}'" for k in JOB_KINDS))

SCHEMA = JOBS_TABLE + """;

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
    error              TEXT,
    gate_source        TEXT,
    queue_at_send      INTEGER,
    active_at_send     INTEGER,
    queue_wait_ms      INTEGER,
    scheduler_requested_at_ms INTEGER,
    scheduler_admitted_at_ms  INTEGER,
    usage_json         TEXT
);

"""

# Columns added after Phase 1; older databases get them on open.
ADDED_COLUMNS = {
    "requests": {
        "gate_source": "TEXT", "queue_at_send": "INTEGER", "active_at_send": "INTEGER",
        "queue_wait_ms": "INTEGER", "scheduler_requested_at_ms": "INTEGER", "scheduler_admitted_at_ms": "INTEGER",
        "slots_at_send": "INTEGER", "usage_json": "TEXT",
    },
    "jobs": {"base_commit": "TEXT", "model": "TEXT", "preferred_model": "TEXT", "route_reason": "TEXT",
             "fallback": "INTEGER", "output_dir": "TEXT"},
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
    base_commit: str | None = None  # the commit the job's branch started from
    # Wait-aware routing (router.py): the model the job runs on for all its steps, the model the
    # user would otherwise have used, why it was chosen, and whether it fell back after a failed check.
    model: str | None = None
    preferred_model: str | None = None
    route_reason: str | None = None
    fallback: int | None = None
    output_dir: str | None = None  # where its edited files were copied: <repo>/spare-gpu/<date>_<time>


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
    slots_at_send: int | None = None
    # When the request left us; defaults to the time the row is written.
    sent_at: str | None = None
    queue_wait_ms: int | None = None
    scheduler_requested_at_ms: int | None = None
    scheduler_admitted_at_ms: int | None = None
    # The reply's whole `usage` block as JSON, so fields we don't parse (cached tokens, say) are kept.
    usage_json: str | None = None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":  # ":memory:": a throwaway store, e.g. for advice in a fresh folder
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Keep spare-gpu's own folder out of whatever repo it is created in. Only that folder:
            # a GTU_DB pointing elsewhere must not git-ignore someone's whole directory.
            marker = self.path.parent / ".gitignore"
            if self.path.parent.name == ".gtu" and not marker.exists():
                marker.write_text("*\n", encoding="utf-8")
        # Several processes share this file: `gtu start`, other gtu commands, and the MCP server.
        # Wait up to 30 s for another writer instead of failing at once, and use WAL so readers
        # never block the writer (or the other way round).
        self.conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # another process holds a lock right now; WAL is persistent, so it's on next time
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.conn.commit()
        self._widen_job_kinds()

    def _widen_job_kinds(self) -> None:
        """An older store limits `kind` with a CHECK constraint that lacks newer kinds (fix). SQLite
        can't change a constraint in place, so the jobs table is rebuilt once, keeping every row;
        the steps and requests that point at jobs by id are untouched."""
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
        if row is None or all(f"'{kind}'" in row["sql"] for kind in JOB_KINDS):
            return
        columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")]
        names = ", ".join(columns)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.conn:
                self.conn.execute(JOBS_TABLE.replace("CREATE TABLE IF NOT EXISTS jobs", "CREATE TABLE jobs_new"))
                for name, kind in ADDED_COLUMNS["jobs"].items():
                    if name in columns:
                        self.conn.execute(f"ALTER TABLE jobs_new ADD COLUMN {name} {kind}")
                self.conn.execute(f"INSERT INTO jobs_new ({names}) SELECT {names} FROM jobs")
                self.conn.execute("DROP TABLE jobs")
                self.conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

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
        """Change a job's status. A cancelled job stays cancelled: the user's cancel (from any
        process) always wins over a runner that finishes or pauses the job a moment later."""
        if status not in JOB_STATUSES:
            raise StoreError(f"unknown job status {status!r}")
        self.get_job(job_id)
        with self.conn:
            self.conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND status != 'cancelled'",
                              (status, utc_now(), job_id))
        return self.get_job(job_id)

    def set_base_commit(self, job_id: str, commit: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE jobs SET base_commit = ? WHERE id = ?", (commit, job_id))

    def set_route(self, job_id: str, model: str, preferred: str, reason: str, fallback: bool = False) -> None:
        """Record the model a job runs on (sticky: every step and retry uses it) and why."""
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET model = ?, preferred_model = ?, route_reason = ?, fallback = ? WHERE id = ?",
                (model, preferred, reason, int(fallback), job_id))

    def set_output_dir(self, job_id: str, folder: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE jobs SET output_dir = ? WHERE id = ?", (folder, job_id))

    def cancel_job(self, job_id: str) -> Job:
        """Cancel in one statement, so a runner finishing the job at the same moment can't
        leave it both done and cancelled."""
        self.get_job(job_id)
        marks = ", ".join("?" for _ in FINAL_STATUSES)
        with self.conn:
            changed = self.conn.execute(
                f"UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND status NOT IN ({marks})",
                (utc_now(), job_id, *FINAL_STATUSES),
            ).rowcount
        job = self.get_job(job_id)
        if not changed:
            raise StoreError(f"job {job_id} is already {job.status}")
        return job

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

    def reset_steps(self, job_id: str) -> None:
        """Make every step of a job pending again (the router's one fallback rerun)."""
        with self.conn:
            self.conn.execute("UPDATE job_steps SET status = 'pending', started_at = NULL, finished_at = NULL "
                              "WHERE job_id = ?", (job_id,))

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
                       total_tokens, finish_reason, truncated, error, gate_source, queue_at_send, active_at_send,
                       queue_wait_ms, scheduler_requested_at_ms, scheduler_admitted_at_ms, slots_at_send, usage_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.job_id, entry.step_id, entry.sent_at or utc_now(), entry.endpoint, entry.model,
                    entry.attempt, entry.status_code, entry.elapsed_ms, entry.request_id, entry.prompt_tokens,
                    entry.completion_tokens, entry.reasoning_tokens, entry.total_tokens,
                    entry.finish_reason, int(entry.truncated), entry.error,
                    entry.gate_source, entry.queue_at_send, entry.active_at_send,
                    entry.queue_wait_ms, entry.scheduler_requested_at_ms, entry.scheduler_admitted_at_ms,
                    entry.slots_at_send, entry.usage_json,
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

    def step_token_history(self, kind: str, step_name: str) -> list[int]:
        """Tokens each finished step of this kind and name used in earlier jobs."""
        rows = self.conn.execute(
            """SELECT SUM(r.total_tokens) FROM requests r
               JOIN job_steps s ON r.step_id = s.id JOIN jobs j ON s.job_id = j.id
               WHERE j.kind = ? AND s.name = ? AND s.status = 'done'
               GROUP BY s.id""",
            (kind, step_name),
        ).fetchall()
        return [r[0] for r in rows]

    def tokens_since(self, since_utc_iso: str) -> int:
        """Tokens used by requests sent at or after `since_utc_iso` (an ISO UTC timestamp)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM requests WHERE sent_at >= ?", (since_utc_iso,)
        ).fetchone()
        return row[0]

    def recent_latencies(self, limit: int) -> list[float]:
        """Response times (ms) of the most recent successful requests."""
        rows = self.conn.execute(
            "SELECT elapsed_ms FROM requests WHERE status_code = 200 AND elapsed_ms IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

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
