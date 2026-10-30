"""Should heavy work run now, and if not, when? (`spare_capacity_advice`, `gtu advice`)

Three signals, each labelled in the answer so the user can weigh them:
- now: the capacity gate's live reading of BharatCode's load;
- the past week: BharatCode's public hourly history for the model's API lane (undocumented, so
  it's context, never the decision itself);
- your own measured waits: how long BharatCode held our requests (x-bharatcode-queue-wait-ms),
  by hour of day.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from .budget import IST
from .gate import Decision
from .store import Store

HISTORY_URL = "https://bharatcode.ai/api/serving/history"
QUIET_MARGIN_PCT = 10.0  # an hour within this many points of the quietest counts as quiet too


def fetch_history(http: httpx.Client, model: str, timeout: float = 10.0) -> list[dict] | None:
    """The past 7 days in hourly buckets, or None if the endpoint is unusable."""
    try:
        response = http.get(HISTORY_URL, params={"hours": 168, "bucket": "1h", "model": model, "lane": "api"},
                            timeout=timeout)
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    buckets = data.get("buckets") if isinstance(data, dict) else None
    if not isinstance(buckets, list):
        return None
    needed = ("started_at", "avg_capacity_utilization_pct", "avg_queue_depth", "sample_count")
    good = [b for b in buckets if isinstance(b, dict) and all(k in b for k in needed) and b["sample_count"]]
    return good or None


def busy_by_hour(buckets: list[dict]) -> dict[int, tuple[float, float]]:
    """IST hour -> (average % of capacity in use, average queue depth)."""
    use, queue = defaultdict(list), defaultdict(list)
    for b in buckets:
        try:
            hour = datetime.fromisoformat(b["started_at"].replace("Z", "+00:00")).astimezone(IST).hour
            use[hour].append(float(b["avg_capacity_utilization_pct"]))
            queue[hour].append(float(b["avg_queue_depth"]))
        except (TypeError, ValueError):
            continue
    return {h: (statistics.mean(use[h]), statistics.mean(queue[h])) for h in use}


def waits_by_hour(store: Store) -> dict[int, list[int]]:
    """IST hour -> BharatCode's reported queue waits (ms) for our own requests."""
    out = defaultdict(list)
    for sent_at, wait in store.conn.execute("SELECT sent_at, queue_wait_ms FROM requests WHERE queue_wait_ms IS NOT NULL"):
        try:
            out[datetime.fromisoformat(sent_at).astimezone(IST).hour].append(int(wait))
        except (TypeError, ValueError):
            continue
    return dict(out)


def _hour(h: int) -> str:
    return f"{h:02d}:00"


def advise(decision: Decision, history: dict[int, tuple[float, float]] | None,
           waits: dict[int, list[int]], now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    r = decision.reading
    # The current reading, not the gate's open/closed: the gate waits for several quiet readings
    # in a row before sending work, which is right for unattended jobs but not for "is it quiet now?".
    good_now = r.verdict == "quiet"
    if good_now:
        lines = [f"Yes, now is a good time: BharatCode has spare capacity ({r.reason})."]
    elif r.verdict == "busy":
        lines = [f"Not right now: {r.reason}."]
    else:
        lines = [f"Can't tell right now ({r.reason})."]

    quiet = None
    best_hours: list[int] = []
    if history:
        ranked = sorted(history.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        quiet = [h for h, _ in ranked[:3]]
        # Hours genuinely close to the quietest one, not just the next in line.
        best_hours = [h for h, (busy_pct, _) in ranked if busy_pct <= ranked[0][1][0] + QUIET_MARGIN_PCT]
        busy = [h for h, _ in ranked[-3:][::-1]]
        lines.append("Past week (BharatCode's hourly stats, IST): quietest around "
                     + ", ".join(f"{_hour(h)} ({history[h][0]:.0f}% busy, queue {history[h][1]:.1f})" for h in quiet)
                     + "; busiest around " + ", ".join(f"{_hour(h)} ({history[h][0]:.0f}% busy)" for h in busy) + ".")
    else:
        lines.append("BharatCode's history wasn't available, so there's no weekly pattern to go on.")

    if waits:
        mine = [w for ws in waits.values() for w in ws]
        here = waits.get(now.hour, [])
        line = (f"Your own requests waited a median {statistics.median(mine) / 1000:.1f} s in BharatCode's queue "
                f"({len(mine)} measured)")
        if len(here) >= 2:
            line += f"; around {_hour(now.hour)} it was {statistics.median(here) / 1000:.1f} s"
        lines.append(line + ".")

    if not good_now:
        if best_hours:
            upcoming = sorted(best_hours, key=lambda h: (h - now.hour) % 24)
            lines.append(f"Best bet: around {_hour(upcoming[0])} IST. Or queue it now (spare_queue_work, or "
                         "spare_run_when_free for any command) and it starts the moment a slot frees up.")
        else:
            lines.append("Queue it now (spare_queue_work, or spare_run_when_free for any command) and it starts "
                         "the moment a slot frees up.")
    return "\n".join(lines)
