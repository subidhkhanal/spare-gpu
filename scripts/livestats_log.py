"""Append one row per model of BharatCode live stats to a CSV per model.

One request to the public /api/serving/usage endpoint per run, so logging more
models costs no extra calls. Each row has that model's api lane figures (the
original columns), then the top-level totals and every model's api lane side by
side. Run it once a minute from a scheduler (Task Scheduler, cron). Standard
library only, so it runs on any Python 3.8+ without the project installed.
Sends no API key. Stops writing after STOP_AFTER.

Usage: python livestats_log.py <output-folder>
Writes <output-folder>/qwen-api-usage.csv and deepseek-api-usage.csv.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

URL = "https://bharatcode.ai/api/serving/usage"
# model id -> (file, column prefix)
MODELS = {
    "qwen-3.8-27b": ("qwen-api-usage.csv", "qwen"),
    "deepseek-v4.1-flash": ("deepseek-api-usage.csv", "deepseek"),
}
IST = timezone(timedelta(hours=5, minutes=30))
STOP_AFTER = datetime(2026, 11, 3, 23, 59, 59, tzinfo=IST)

LANE_FIELDS = ("available", "active_requests", "queued_requests", "slot_capacity", "capacity_used_pct")
# The original columns, kept first and unchanged; new columns are only ever appended.
BASE_FIELDS = [
    "fetched_utc", "fetched_ist", "http_status", "response_ms", "generated_at",
    *LANE_FIELDS, "total_active", "total_queued", "error",
]
FIELDS = [
    *BASE_FIELDS,
    "total_slot_capacity", "total_capacity_used_pct",
    *(f"{prefix}_{field}" for _, prefix in MODELS.values() for field in LANE_FIELDS),
]


def fetch() -> tuple[dict, dict | None]:
    """Fields shared by every model's row for this run, plus the parsed body (None on failure)."""
    now = datetime.now(timezone.utc)
    shared = {f: "" for f in FIELDS}
    shared["fetched_utc"] = now.isoformat(timespec="seconds")
    shared["fetched_ist"] = now.astimezone(IST).isoformat(timespec="seconds")
    request = urllib.request.Request(URL, headers={"User-Agent": "spare-gpu-livestats-log/0.3"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            shared["http_status"] = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        shared["http_status"] = exc.code
        shared["error"] = "http error"
        return shared, None
    except Exception as exc:  # network down, timeout, DNS: record and move on
        shared["error"] = type(exc).__name__
        return shared, None
    finally:
        shared["response_ms"] = round((time.perf_counter() - start) * 1000)

    try:
        data = json.loads(body)
        shared["generated_at"] = data.get("generated_at", "")
        shared["total_active"] = data.get("active_requests", "")
        shared["total_queued"] = data.get("queued_requests", "")
        shared["total_slot_capacity"] = data.get("slot_capacity", "")
        shared["total_capacity_used_pct"] = data.get("capacity_used_pct", "")
        by_id = {m.get("id"): m for m in data.get("models", [])}
        for model_id, (_, prefix) in MODELS.items():
            lane = (by_id.get(model_id) or {}).get("api") or {}
            for field in LANE_FIELDS:
                shared[f"{prefix}_{field}"] = lane.get(field, "")
        return shared, data
    except (ValueError, AttributeError, TypeError):
        shared["error"] = "unexpected body"
        return shared, None


def model_row(shared: dict, data: dict | None, model_id: str) -> dict:
    """This model's row: its own lane in the original columns, everything else shared."""
    row = dict(shared)
    if data is None:
        return row
    try:
        model = next((m for m in data.get("models", []) if m.get("id") == model_id), None)
        if model is None:
            row["error"] = f"{model_id} missing"
            return row
        lane = model.get("api") or {}
        if not lane:
            row["error"] = "api lane missing"
        for field in LANE_FIELDS:
            row[field] = lane.get(field, "")
    except (AttributeError, TypeError):
        row["error"] = "unexpected body"
    return row


def upgrade_header(path: Path) -> None:
    """If the file has an older, shorter header, rewrite it with the new columns (blank in old rows)."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        if header == FIELDS:
            return
        rows = list(reader)
    if header != FIELDS[: len(header)]:
        # Not a header we wrote; keep it aside rather than guess.
        path.rename(path.with_name(f"{path.stem}.unrecognised-{int(time.time())}{path.suffix}"))
        return
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow(row + [""] * (len(FIELDS) - len(row)))
    os.replace(tmp, path)


def append(path: Path, row: dict) -> None:
    if path.exists() and path.stat().st_size > 0:
        upgrade_header(path)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main(folder: Path) -> None:
    if datetime.now(timezone.utc) > STOP_AFTER:
        return
    folder.mkdir(parents=True, exist_ok=True)
    shared, data = fetch()
    for model_id, (filename, _) in MODELS.items():
        append(folder / filename, model_row(shared, data, model_id))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python livestats_log.py <output-folder>")
    main(Path(sys.argv[1]))
