"""Append one row per model of BharatCode live stats (api lane) to a CSV per model.

One request to the public /api/serving/usage endpoint per run, so logging more
models costs no extra calls. Run it once a minute from a scheduler (Task
Scheduler, cron). Standard library only, so it runs on any Python 3.8+ without
the project installed. Sends no API key. Stops writing after STOP_AFTER.

Usage: python livestats_log.py <output-folder>
Writes <output-folder>/qwen-api-usage.csv and deepseek-api-usage.csv.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

URL = "https://bharatcode.ai/api/serving/usage"
MODELS = {
    "qwen-3.8-27b": "qwen-api-usage.csv",
    "deepseek-v4.1-flash": "deepseek-api-usage.csv",
}
IST = timezone(timedelta(hours=5, minutes=30))
STOP_AFTER = datetime(2026, 11, 3, 23, 59, 59, tzinfo=IST)

FIELDS = [
    "fetched_utc", "fetched_ist", "http_status", "response_ms", "generated_at",
    "available", "active_requests", "queued_requests", "slot_capacity", "capacity_used_pct",
    "total_active", "total_queued", "error",
]
LANE_FIELDS = ("available", "active_requests", "queued_requests", "slot_capacity", "capacity_used_pct")


def fetch() -> tuple[dict, dict | None]:
    """Shared row fields for this run, plus the parsed body (None if the request failed)."""
    now = datetime.now(timezone.utc)
    base = {f: "" for f in FIELDS}
    base["fetched_utc"] = now.isoformat(timespec="seconds")
    base["fetched_ist"] = now.astimezone(IST).isoformat(timespec="seconds")
    request = urllib.request.Request(URL, headers={"User-Agent": "spare-gpu-livestats-log/0.2"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            base["http_status"] = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        base["http_status"] = exc.code
        base["error"] = "http error"
        return base, None
    except Exception as exc:  # network down, timeout, DNS: record and move on
        base["error"] = type(exc).__name__
        return base, None
    finally:
        base["response_ms"] = round((time.perf_counter() - start) * 1000)
    try:
        data = json.loads(body)
        base["generated_at"] = data.get("generated_at", "")
        base["total_active"] = data.get("active_requests", "")
        base["total_queued"] = data.get("queued_requests", "")
        return base, data
    except (ValueError, AttributeError, TypeError):
        base["error"] = "unexpected body"
        return base, None


def model_row(base: dict, data: dict | None, model_id: str) -> dict:
    row = dict(base)
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
        for key in LANE_FIELDS:
            row[key] = lane.get(key, "")
    except (AttributeError, TypeError):
        row["error"] = "unexpected body"
    return row


def append(path: Path, row: dict) -> None:
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
    base, data = fetch()
    for model_id, filename in MODELS.items():
        append(folder / filename, model_row(base, data, model_id))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python livestats_log.py <output-folder>")
    main(Path(sys.argv[1]))
