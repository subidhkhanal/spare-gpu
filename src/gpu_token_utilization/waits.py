"""How long work would wait on each BharatCode model right now (`spare_waits`, `gtu waits`, the router).

Four signals per model, each optional; a failure means "no data", never "no wait":
- BharatCode's measured queue waits for everyone, last 60 min (GET /api/serving/queue-waits: p50/p75/p90);
- the live lane: is a slot free, is anything queued (GET /api/serving/usage);
- our own recent measured waits on that model (x-bharatcode-queue-wait-ms, in the requests table),
  which catch account-level pacing the global figures can't;
- what our own waits usually are at this IST hour.
The estimate is deliberately simple and explained, because what the router needs is the right
ranking of the models, not a precise number: a free slot with an empty queue means ~0; otherwise a
blend of the global p75 and our own recent waits; else the hour's usual; else unknown.
"""

from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .budget import IST
from .store import Store

USAGE_URL = "https://bharatcode.ai/api/serving/usage"


class LaneStats(BaseModel):
    """One model's API lane in BharatCode's live stats."""
    model_config = ConfigDict(strict=True, extra="ignore")
    active_requests: int = Field(ge=0)
    queued_requests: int = Field(ge=0)
    slot_capacity: int = Field(ge=0)
    available: bool


class ModelStats(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    id: str
    api: LaneStats | None = None


class UsageSnapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    generated_at: datetime
    models: list[ModelStats]

QUEUE_WAITS_URL = "https://bharatcode.ai/api/serving/queue-waits"
MODELS_ENV = "GTU_MODELS"
DEFAULT_MODELS = ("deepseek-v4.1-flash", "qwen-3.8-27b")  # the two in BharatCode's catalog, heavy first
NAMES = {"deepseek-v4.1-flash": "DeepSeek V4.1 Flash", "qwen-3.8-27b": "Qwen 3.8 27B"}
CACHE_S = 60.0  # polite: each endpoint at most once a minute
MAX_AGE_S = 180.0  # older figures are treated as missing
RECENT = timedelta(minutes=60)
RECENT_COUNT = 5
GLOBAL_WEIGHT = 0.6  # in the blend; our own waits get the rest
MIN_HOUR = 3


class ModelWaits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    sample_count: int = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p75_ms: float = Field(ge=0)
    p90_ms: float = Field(ge=0)


class QueueWaits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    window_minutes: int = Field(gt=0)
    as_of: datetime
    models: list[ModelWaits]


def model_id(model: str | None) -> str:
    """'bharatcode/qwen-3.8-27b' and 'qwen-3.8-27b' are the same model."""
    return (model or "").removeprefix("bharatcode/")


def models() -> list[str]:
    configured = [m.strip() for m in os.environ.get(MODELS_ENV, "").split(",") if m.strip()]
    return [model_id(m) for m in configured] or list(DEFAULT_MODELS)


def name(model: str) -> str:
    return NAMES.get(model_id(model), model_id(model))


# --- fetching, cached and validated -------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fetch: Callable[[], object], clock: Callable[[], float]) -> object:
    hit = _cache.get(key)
    if hit and clock() - hit[0] < CACHE_S:
        return hit[1]
    value = fetch()
    _cache[key] = (clock(), value)
    return value


def queue_waits(http: httpx.Client, now: datetime | None = None,
                clock: Callable[[], float] = time.monotonic) -> dict[str, ModelWaits] | None:
    """BharatCode's measured waits per model, or None if the endpoint is unusable or stale."""
    def fetch():
        try:
            response = http.get(QUEUE_WAITS_URL, timeout=10.0)
            if response.status_code != 200:
                return None
            data = QueueWaits.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError, ValueError):
            return None
        as_of = data.as_of if data.as_of.tzinfo else data.as_of.replace(tzinfo=timezone.utc)
        if abs(((now or datetime.now(timezone.utc)) - as_of).total_seconds()) > MAX_AGE_S:
            return None
        return {m.id: m for m in data.models}
    return _cached(QUEUE_WAITS_URL, fetch, clock)


def lane_state(http: httpx.Client, clock: Callable[[], float] = time.monotonic) -> dict[str, LaneStats] | None:
    """Each model's API lane right now, or None if the live stats are unusable."""
    def fetch():
        try:
            response = http.get(USAGE_URL, timeout=10.0)
            if response.status_code != 200:
                return None
            snapshot = UsageSnapshot.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError, ValueError):
            return None
        return {m.id: m.api for m in snapshot.models if m.api is not None}
    return _cached(USAGE_URL, fetch, clock)


def own_waits(store: Store, model: str, now: datetime) -> tuple[list[float], list[float]]:
    """(our recent waits on this model, our waits at this IST hour on any day), in seconds."""
    recent, hour = [], []
    rows = store.conn.execute(
        "SELECT sent_at, model, queue_wait_ms FROM requests WHERE queue_wait_ms IS NOT NULL AND status_code = 200 "
        "ORDER BY sent_at")
    for sent_at, row_model, wait in rows:
        if model_id(row_model) != model_id(model):
            continue
        try:
            when = datetime.fromisoformat(sent_at)
        except (TypeError, ValueError):
            continue
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if now - when <= RECENT:
            recent.append(wait / 1000)
        if when.astimezone(IST).hour == now.astimezone(IST).hour:
            hour.append(wait / 1000)
    return recent[-RECENT_COUNT:], hour


# --- the estimate ---------------------------------------------------------------------------------

@dataclass
class View:
    model: str
    global_waits: ModelWaits | None = None
    lane: LaneStats | None = None
    recent: list[float] = field(default_factory=list)
    usual: list[float] = field(default_factory=list)

    @property
    def free_slot(self) -> bool | None:
        if self.lane is None:
            return None
        return self.lane.available and self.lane.active_requests < self.lane.slot_capacity and self.lane.queued_requests == 0


@dataclass
class Estimate:
    model: str
    seconds: float | None  # None: no data at all
    basis: str


def estimate(view: View) -> Estimate:
    g = view.global_waits
    if view.lane is not None and not view.lane.available:
        return Estimate(view.model, None, "model unavailable right now")
    if view.free_slot:
        floor = g.p50_ms / 1000 if g else 0.0
        return Estimate(view.model, floor, "free slot, empty queue")
    parts, basis = [], []
    if g and g.sample_count:
        parts.append((GLOBAL_WEIGHT, g.p75_ms / 1000))
        basis.append(f"p75 last hour {_secs(g.p75_ms / 1000)}")
    if view.recent:
        parts.append((1 - GLOBAL_WEIGHT, statistics.median(view.recent)))
        basis.append(f"your last {len(view.recent)} waits {_secs(min(view.recent))}–{_secs(max(view.recent))}")
    if parts:
        total = sum(w for w, _ in parts)
        return Estimate(view.model, sum(w * s for w, s in parts) / total, "; ".join(basis))
    if len(view.usual) >= MIN_HOUR:
        usual = statistics.median(view.usual)
        return Estimate(view.model, usual, f"usual for this hour {_secs(usual)} ({len(view.usual)} of your requests)")
    return Estimate(view.model, None, "no wait data")


def _secs(seconds: float) -> str:
    if seconds < 1:
        return "0 s"
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds // 60:.0f}m {seconds % 60:02.0f}s"


def views(store: Store, http: httpx.Client, now: datetime | None = None, wanted: list[str] | None = None,
          clock: Callable[[], float] = time.monotonic) -> list[View]:
    now = now or datetime.now(timezone.utc)
    global_waits = queue_waits(http, now, clock) or {}
    lanes = lane_state(http, clock) or {}
    out = []
    for model in wanted or models():
        recent, usual = own_waits(store, model, now)
        out.append(View(model, global_waits.get(model), lanes.get(model), recent, usual))
    return out


def explain(est: Estimate) -> str:
    when = "unknown" if est.seconds is None else f"≈ {_secs(est.seconds)}"
    return f"{name(est.model)} {when} ({est.basis})"


def describe(all_views: list[View]) -> str:
    """The `spare_waits` answer: one line per model, then a one-line recommendation."""
    lines = []
    estimates = []
    for view in all_views:
        est = estimate(view)
        estimates.append(est)
        bits = []
        if view.global_waits:
            g = view.global_waits
            bits.append(f"everyone, last hour: p50 {_secs(g.p50_ms / 1000)}, p75 {_secs(g.p75_ms / 1000)} "
                        f"({g.sample_count} requests)")
        if view.lane is not None:
            slot = "free slot" if view.free_slot else "no free slot"
            bits.append(f"{slot} ({view.lane.active_requests}/{view.lane.slot_capacity} busy, "
                        f"{view.lane.queued_requests} queued)")
        if view.recent:
            bits.append(f"your recent waits: {', '.join(_secs(w) for w in view.recent)}")
        if len(view.usual) >= MIN_HOUR:
            bits.append(f"usual for you at this hour: {_secs(statistics.median(view.usual))}")
        lines.append(f"{name(view.model)}: {'; '.join(bits) or 'no data right now'}.")
        lines.append(f"  Estimated wait if sent now: {'unknown' if est.seconds is None else _secs(est.seconds)}.")
    known = sorted((e for e in estimates if e.seconds is not None), key=lambda e: e.seconds)
    if len(known) >= 2 and known[-1].seconds - known[0].seconds >= 20:
        lines.append(f"{name(known[0].model)} would start about {_secs(known[-1].seconds - known[0].seconds)} sooner "
                     f"than {name(known[-1].model)} right now. spare_delegate hands a switch-safe task to the faster one.")
    elif len(known) >= 2:
        lines.append("No meaningful difference between the models right now.")
    else:
        lines.append("Not enough data to compare the models right now.")
    return "\n".join(lines)
