"""Gate-vs-send experiment: does waiting for the gate beat just sending?

Each run takes one paired sample (or a burst of N back-to-back samples):
  1. read BharatCode's live stats for the Qwen api lane,
  2. note what the gate would decide right now (default and strict rules), without waiting,
  3. send one small identical request immediately, regardless of load,
  4. append the live stats, the would-be decision, the server-reported queue wait and the
     total time to a CSV.

Requests go through the project's own client (no retries, 180 s timeout), so they are
logged like any other. One request at a time: a lock file stops overlapping runs.
Needs BHARATCODE_API_KEY in the environment; never prints it.

Usage:
  python gate_vs_send.py <out-folder>              # one paired sample
  python gate_vs_send.py <out-folder> --burst 5    # five in a row, no pause
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from gpu_token_utilization.client import BharatCodeClient, BharatCodeError, RetryPolicy
from gpu_token_utilization.gate import USAGE_URL, GateConfig, UsageSnapshot, lane_verdict
from gpu_token_utilization.store import Store

IST = timezone(timedelta(hours=5, minutes=30))
MODEL = "qwen-3.8-27b"
MESSAGES = [{"role": "user", "content": "Reply with exactly the word: ok"}]
LOCK_MAX_AGE = 15 * 60  # a lock older than this is from a crashed run

FIELDS = [
    "sample_utc", "sample_ist", "kind", "burst_index",
    "usage_error", "available", "active", "queued", "slots", "total_active", "total_queued",
    "verdict_default", "verdict_strict", "gate_reason",
    "sent_at", "status", "total_ms", "queue_wait_ms", "scheduler_requested_at_ms", "scheduler_admitted_at_ms",
    "request_id", "prompt_tokens", "completion_tokens", "total_tokens", "error",
]


def read_usage(row: dict) -> None:
    try:
        response = httpx.get(USAGE_URL, timeout=10)
        snapshot = UsageSnapshot.model_validate_json(response.content)
        raw = response.json()
    except (httpx.HTTPError, ValidationError, ValueError) as exc:
        row["usage_error"] = type(exc).__name__
        return
    model = next((m for m in snapshot.models if m.id == MODEL), None)
    if model is None or model.api is None:
        row["usage_error"] = "model or api lane missing"
        return
    lane = model.api
    row.update(
        available=lane.available, active=lane.active_requests, queued=lane.queued_requests,
        slots=lane.slot_capacity, total_active=raw.get("active_requests", ""), total_queued=raw.get("queued_requests", ""),
    )
    default_cfg = GateConfig()
    row["verdict_default"], row["gate_reason"] = lane_verdict(lane, default_cfg)
    row["verdict_strict"], _ = lane_verdict(lane, GateConfig(max_queued=0))


def send(client: BharatCodeClient, store: Store, row: dict) -> None:
    before = store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM requests").fetchone()[0]
    try:
        client.chat(MESSAGES, max_tokens=8, reasoning_effort="none", gate={"gate_source": "experiment"})
    except BharatCodeError as exc:
        row["error"] = str(exc)[:200]
    logged = store.conn.execute("SELECT * FROM requests WHERE id > ? ORDER BY id DESC LIMIT 1", (before,)).fetchone()
    if logged is None:
        return
    row.update(
        sent_at=logged["sent_at"], status=logged["status_code"],
        total_ms=round(logged["elapsed_ms"]) if logged["elapsed_ms"] is not None else "",
        queue_wait_ms=logged["queue_wait_ms"], scheduler_requested_at_ms=logged["scheduler_requested_at_ms"],
        scheduler_admitted_at_ms=logged["scheduler_admitted_at_ms"], request_id=logged["request_id"],
        prompt_tokens=logged["prompt_tokens"], completion_tokens=logged["completion_tokens"],
        total_tokens=logged["total_tokens"],
    )
    if not row["error"] and logged["error"]:
        row["error"] = logged["error"]


def take(client: BharatCodeClient, store: Store, kind: str, index: int) -> dict:
    now = datetime.now(timezone.utc)
    row = {f: "" for f in FIELDS}
    row.update(sample_utc=now.isoformat(timespec="seconds"), sample_ist=now.astimezone(IST).isoformat(timespec="seconds"),
               kind=kind, burst_index=index)
    read_usage(row)
    send(client, store, row)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--burst", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    lock = args.out / "gate-vs-send.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_MAX_AGE:
        return  # another run is mid-request; never two in flight
    lock.write_text(str(os.getpid()))
    try:
        with Store(args.out / "experiment.db") as store, \
                BharatCodeClient(store=store, retry=RetryPolicy(max_retries=0)) as client:
            if args.burst:
                rows = [take(client, store, "burst", i + 1) for i in range(args.burst)]
            else:
                rows = [take(client, store, "paired", 0)]
        path = args.out / "gate-vs-send.csv"
        is_new = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerows(rows)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
