"""Is now a fine time for heavy work on BharatCode? (measured waits, for `gtu advice`)

Built on what we measured, not on forecasts. BharatCode reports how long it held each of our
requests (x-bharatcode-queue-wait-ms); the answer is those waits:
- now: the median of our last few requests, if they're recent, and how many it rests on;
- trend: whether waits over the last ~20 minutes are rising, falling or steady;
- this hour: what our requests usually waited at this time of day.
BharatCode's live lane figures are shown for context only. In our experiment its queue count
tracked waits roughly at 4 slots and not at all at 2, and our own recent waits didn't predict the
next one better either (research/why-live-stats-dont-predict-wait.md). So this describes, and
never claims to know what the next request will wait.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .budget import IST
from .store import Store

RECENT = timedelta(minutes=30)  # older than this isn't "now"
RECENT_COUNT = 5  # the last this many requests, within RECENT
MIN_RECENT = 3  # fewer than this and we say we don't have enough
TREND_WINDOW = timedelta(minutes=20)
MIN_TREND = 4
ELEVATED_S = 30.0  # median waits above this are called elevated (the experiment's p90 was 56 s)
MIN_HOUR = 3  # samples needed before quoting an hour of day


@dataclass
class Wait:
    sent_at: datetime
    seconds: float


def measured_waits(store: Store) -> list[Wait]:
    """Our successful requests that came back with BharatCode's queue-wait figure, oldest first."""
    out = []
    rows = store.conn.execute(
        "SELECT sent_at, queue_wait_ms FROM requests WHERE queue_wait_ms IS NOT NULL AND status_code = 200"
    )
    for sent_at, wait in rows:
        try:
            when = datetime.fromisoformat(sent_at)
        except (TypeError, ValueError):
            continue
        out.append(Wait(when if when.tzinfo else when.replace(tzinfo=timezone.utc), int(wait) / 1000))
    return sorted(out, key=lambda w: w.sent_at)


def _s(seconds: float) -> str:
    return f"{seconds:.1f} s"


def _ago(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60
    return "under a minute ago" if minutes < 1 else f"{minutes:.0f} min ago"


def trend(waits: list[Wait], now: datetime) -> str | None:
    """'rising', 'falling' or 'steady' over the last TREND_WINDOW, or None with too few samples."""
    window = [w.seconds for w in waits if now - w.sent_at <= TREND_WINDOW]
    if len(window) < MIN_TREND:
        return None
    half = len(window) // 2
    before, after = statistics.median(window[:half]), statistics.median(window[-half:])
    # Waits jump around by tens of seconds from one request to the next, so only call a big move.
    if after > max(before * 1.5, before + 5):
        return "rising"
    if after < min(before / 1.5, before - 5):
        return "falling"
    return "steady"


def advise(waits: list[Wait], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    recent = [w for w in waits if now - w.sent_at <= RECENT][-RECENT_COUNT:]
    local_hour = now.astimezone(IST).hour
    this_hour = [w for w in waits if w.sent_at.astimezone(IST).hour == local_hour]
    hour_days = {w.sent_at.astimezone(IST).date() for w in this_hour}

    lines = []
    if len(recent) >= MIN_RECENT:
        median = statistics.median(w.seconds for w in recent)
        basis = (f"median {_s(median)} over our last {len(recent)} requests, "
                 f"the newest {_ago(now - recent[-1].sent_at)}")
        if median > ELEVATED_S:
            lines.append(f"Waits are elevated right now: {basis}. Work still runs, just slower; "
                         "BharatCode's busy answers are retried.")
        else:
            lines.append(f"Fine to run now: {basis}.")
        direction = trend(waits, now)
        if direction:
            lines.append(f"Over the last {TREND_WINDOW.seconds // 60} minutes waits look {direction}.")
        else:
            lines.append("Too few requests in the last 20 minutes to tell whether waits are rising or falling.")
    else:
        if recent:
            lines.append(f"Not enough recent data to say how it is right now: only {len(recent)} of our requests "
                         f"in the last {RECENT.seconds // 60} minutes.")
        else:
            lines.append(f"Not enough recent data to say how it is right now: none of our requests "
                         f"in the last {RECENT.seconds // 60} minutes.")

    if len(this_hour) >= MIN_HOUR:
        lines.append(f"Around {local_hour:02d}:00 IST our requests have waited a median "
                     f"{_s(statistics.median(w.seconds for w in this_hour))} (worst {_s(max(w.seconds for w in this_hour))}), "
                     f"from {len(this_hour)} requests on {len(hour_days)} day(s).")
    elif waits:
        lines.append(f"Too little history for {local_hour:02d}:00 IST to say what's usual then "
                     f"({len(this_hour)} request(s)). Across all {len(waits)} measured, the median wait was "
                     f"{_s(statistics.median(w.seconds for w in waits))}.")
    else:
        lines.append("No measured waits at all yet, so we don't know what's usual. spare-gpu sends anyway and "
                     "retries busy answers, so it's safe to queue work now.")

    lines.append("To run something: spare_delegate for tests, docs, a review or a fix.")
    return "\n".join(lines)
