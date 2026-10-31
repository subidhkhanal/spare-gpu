"""Capacity gate: decides whether BharatCode is quiet enough to send the next job step.

The primary signal is the public live stats endpoint (/api/serving/usage). It is
undocumented, so every reading is validated strictly and anything unexpected
falls back to our own request history plus an occasional tiny probe. Decisions
change slowly: several quiet readings in a row to open, fewer busy ones to close.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .client import BUSY_STATUSES, DEFAULT_MODEL, BharatCodeClient, BharatCodeError
from .store import GateReading, Store

USAGE_URL = "https://bharatcode.ai/api/serving/usage"
IST = timezone(timedelta(hours=5, minutes=30))
SOURCE_LABELS = {"livestats": "live stats", "fallback": "fallback probes", "own": "own request", "bypass": "not waiting"}
# Waiting for a free slot is opt-in: measured, it cost hours where sending cost seconds
# (research/gate-vs-send-experiment.md).
WAIT_ENV = "GTU_WAIT_FOR_CAPACITY"
BYPASS_PREFIX = "not waiting for capacity; "


# Only the fields the gate relies on. Strict: a changed type means the shape changed.
class LaneStats(BaseModel):
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


def parse_window(text: str) -> tuple[time, time]:
    """'00:00-09:00' -> (00:00, 09:00), in IST. Equal start and end means all day."""
    start, end = (time.fromisoformat(part.strip()) for part in text.split("-"))
    return start, end


def in_window(now: datetime, window: tuple[time, time]) -> bool:
    start, end = window
    t = now.astimezone(IST).time()
    if start == end:
        return True
    if start < end:
        return start <= t < end
    return t >= start or t < end


def window_label(window: tuple[time, time]) -> str:
    return f"{window[0]:%H:%M}-{window[1]:%H:%M} IST"


@dataclass
class GateConfig:
    model: str = DEFAULT_MODEL
    poll_interval: float = 60.0
    quiet_needed: int = 3
    busy_needed: int = 2
    # BharatCode paces accounts, so a queue can sit there while slots are idle; it is a
    # tolerance, not a blocker. 0 restores the original strict rule; None ignores the queue.
    max_queued: int | None = 5
    max_skew: float = 180.0
    usage_timeout: float = 5.0
    window: tuple[time, time] = (time(0, 0), time(9, 0))
    window_always: bool = False
    probe_interval: float = 300.0
    probe_after_idle: float = 600.0
    fallback_sample: int = 5
    fallback_busy_count: int = 2
    latency_factor: float = 3.0
    baseline_sample: int = 50

    @classmethod
    def from_env(cls) -> GateConfig:
        cfg = cls()
        if os.environ.get("GTU_WINDOW"):
            cfg.window = parse_window(os.environ["GTU_WINDOW"])
        cfg.window_always = os.environ.get("GTU_WINDOW_ALWAYS", "") in ("1", "true", "yes")
        max_queued = os.environ.get("GTU_MAX_QUEUED", "").strip().lower()
        if max_queued in ("none", "off"):
            cfg.max_queued = None
        elif max_queued:
            cfg.max_queued = int(max_queued)
        return cfg


@dataclass
class Signal:
    source: str
    verdict: str  # quiet | busy | unknown | unusable (live stats only, triggers fallback)
    reason: str
    active: int | None = None
    queued: int | None = None
    slots: int | None = None


@dataclass
class Decision:
    open: bool
    reading: GateReading
    next_check: float
    config: GateConfig = field(repr=False)
    bypassed: bool = False  # made by check_bypassed (`gtu start` without --wait-for-capacity)

    @property
    def gate_info(self) -> dict:
        """What to record on requests sent under this decision."""
        r = self.reading
        return {"gate_source": r.source, "queue_at_send": r.queued, "active_at_send": r.active, "slots_at_send": r.slots}

    def describe(self) -> str:
        r = self.reading
        via = "" if r.source in ("livestats", "bypass") else f" ({SOURCE_LABELS.get(r.source, r.source)})"
        if self.open:
            if self.bypassed:
                return f"Sending now (not waiting for capacity): {r.reason.removeprefix(BYPASS_PREFIX)}."
            return f"Gate open{via}: {r.reason}."
        if self.bypassed:
            return f"Paused: {r.reason}. Trying again in {self.next_check:.0f}s (busy answers are still respected)."
        need = self.config.quiet_needed
        return (
            f"Gate closed{via}: {r.reason}. Next check in {self.next_check:.0f}s "
            f"(opens after {need} quiet readings in a row; {r.quiet_streak} so far)."
        )


def lane_verdict(lane: LaneStats, cfg: GateConfig) -> tuple[str, str]:
    """('quiet' or 'busy', reason) for one live-stats reading of a model's api lane."""
    # Quiet means the model is up and a slot is free. The queue only counts beyond the
    # tolerance: under BharatCode's account pacing it can be non-empty while slots sit idle.
    over_queue = cfg.max_queued is not None and lane.queued_requests > cfg.max_queued
    quiet = lane.available and lane.active_requests < lane.slot_capacity and not over_queue
    reason = f"{cfg.model} api lane {lane.active_requests}/{lane.slot_capacity} slots busy, {lane.queued_requests} queued"
    if not lane.available:
        reason += ", model unavailable"
    if over_queue:
        reason += f" (tolerance {cfg.max_queued})"
    return ("quiet" if quiet else "busy"), reason


def _parse_ts(text: str) -> datetime:
    ts = datetime.fromisoformat(text)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class Gate:
    def __init__(
        self,
        store: Store,
        config: GateConfig | None = None,
        *,
        http: httpx.Client | None = None,
        client: BharatCodeClient | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.cfg = config or GateConfig()
        self._http = http or httpx.Client(timeout=self.cfg.usage_timeout)
        self.client = client  # used for fallback probes; None disables probing
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_probe: datetime | None = None

    def close(self) -> None:
        self._http.close()

    def check(self, allow_probe: bool = True) -> Decision:
        """Current decision. Takes a new reading at most once per poll interval."""
        now = self._now()
        last = self.store.last_gate_reading()
        if last is not None:
            age = (now - _parse_ts(last.taken_at)).total_seconds()
            if 0 <= age < self.cfg.poll_interval:
                return Decision(last.decision == "open", last, self.cfg.poll_interval - age, self.cfg)
        signal = self._live(now)
        if signal.verdict == "unusable":
            signal = self._fallback(now, signal.reason, allow_probe)
        elif self.cfg.window_always and not in_window(now, self.cfg.window) and signal.verdict == "quiet":
            signal.verdict = "busy"
            signal.reason += f"; outside the window {window_label(self.cfg.window)}"
        return self._record(now, signal)

    def force_busy(self, reason: str) -> Decision:
        """Close the gate at once, e.g. after BharatCode answered one of our requests with 503."""
        return self._record(self._now(), Signal("own", "busy", reason), force_close=True)

    def check_bypassed(self) -> Decision:
        """For `gtu start` without --wait-for-capacity: always open, but still read live stats (at
        most once per poll interval) so every request records the capacity it was sent into. The
        only pause is a poll interval after BharatCode answered one of our requests busy, so it never hammers."""
        now = self._now()
        last = self.store.last_gate_reading()
        if last is not None:
            age = (now - _parse_ts(last.taken_at)).total_seconds()
            if 0 <= age < self.cfg.poll_interval:
                if last.source == "own":
                    return Decision(False, last, self.cfg.poll_interval - age, self.cfg, bypassed=True)
                if last.source == "bypass":
                    return Decision(True, last, self.cfg.poll_interval - age, self.cfg, bypassed=True)
        live = self._live(now)
        verdict = "unknown" if live.verdict == "unusable" else live.verdict
        reading = self.store.add_gate_reading(
            GateReading(
                taken_at=now.isoformat(), source="bypass", verdict=verdict, decision="open",
                quiet_streak=0, busy_streak=0, reason=BYPASS_PREFIX + live.reason,
                model=self.cfg.model, active=live.active, queued=live.queued, slots=live.slots,
            )
        )
        return Decision(True, reading, self.cfg.poll_interval, self.cfg, bypassed=True)

    def _record(self, now: datetime, signal: Signal, force_close: bool = False) -> Decision:
        last = self.store.last_gate_reading()
        quiet = (last.quiet_streak if last else 0) + 1 if signal.verdict == "quiet" else 0
        busy = (last.busy_streak if last else 0) + 1 if signal.verdict != "quiet" else 0
        was_open = last is not None and last.decision == "open"
        if force_close:
            is_open = False
        elif was_open:
            is_open = busy < self.cfg.busy_needed
        else:
            is_open = quiet >= self.cfg.quiet_needed
        reading = self.store.add_gate_reading(
            GateReading(
                taken_at=now.isoformat(),
                source=signal.source,
                verdict=signal.verdict,
                decision="open" if is_open else "closed",
                quiet_streak=quiet,
                busy_streak=busy,
                reason=signal.reason,
                model=self.cfg.model,
                active=signal.active,
                queued=signal.queued,
                slots=signal.slots,
            )
        )
        return Decision(is_open, reading, self.cfg.poll_interval, self.cfg)

    def _live(self, now: datetime) -> Signal:
        def unusable(why: str) -> Signal:
            return Signal("livestats", "unusable", why)

        try:
            response = self._http.get(USAGE_URL, timeout=self.cfg.usage_timeout)
        except httpx.HTTPError as exc:
            return unusable(f"live stats unreachable ({type(exc).__name__})")
        if response.status_code != 200:
            return unusable(f"live stats returned HTTP {response.status_code}")
        try:
            snapshot = UsageSnapshot.model_validate_json(response.content)
        except ValidationError:
            return unusable("live stats had an unexpected shape")
        generated = snapshot.generated_at
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        skew = abs((now - generated).total_seconds())
        if skew > self.cfg.max_skew:
            return unusable(f"live stats are {skew:.0f} s out of date")
        model = next((m for m in snapshot.models if m.id == self.cfg.model), None)
        if model is None or model.api is None:
            return unusable(f"live stats have no api lane for {self.cfg.model}")

        lane = model.api
        verdict, reason = lane_verdict(lane, self.cfg)
        return Signal(
            "livestats", verdict, reason,
            active=lane.active_requests, queued=lane.queued_requests, slots=lane.slot_capacity,
        )

    def _fallback(self, now: datetime, live_reason: str, allow_probe: bool) -> Signal:
        prefix = f"{live_reason}; "
        if not in_window(now, self.cfg.window):
            return Signal("fallback", "busy", prefix + f"outside the fallback window {window_label(self.cfg.window)}")
        if allow_probe and self.client is not None and self._probe_due(now):
            self._probe(now)

        recent = self.store.recent_requests(self.cfg.fallback_sample)
        if not recent:
            return Signal("fallback", "unknown", prefix + "no recent requests to judge by")
        busy = sum(1 for r in recent if r["status_code"] in BUSY_STATUSES or r["error"] == "timeout")
        if busy >= self.cfg.fallback_busy_count:
            return Signal("fallback", "busy", prefix + f"{busy} of the last {len(recent)} requests were busy or timed out")
        latest = [r["elapsed_ms"] for r in recent if r["status_code"] == 200 and r["elapsed_ms"] is not None]
        baseline = self.store.recent_latencies(self.cfg.baseline_sample)
        if latest and len(baseline) >= 5:
            current, usual = statistics.median(latest), statistics.median(baseline)
            if current > self.cfg.latency_factor * usual:
                return Signal("fallback", "busy", prefix + f"responses are slow ({current:.0f} ms vs usual {usual:.0f} ms)")
        return Signal("fallback", "quiet", prefix + f"last {len(recent)} requests healthy ({busy} busy)")

    def _probe_due(self, now: datetime) -> bool:
        if self._last_probe is not None and (now - self._last_probe).total_seconds() < self.cfg.probe_interval:
            return False
        recent = self.store.recent_requests(1)
        if not recent:
            return True
        return (now - _parse_ts(recent[0]["sent_at"])).total_seconds() >= self.cfg.probe_after_idle

    def _probe(self, now: datetime) -> None:
        self._last_probe = now
        try:
            self.client.chat([{"role": "user", "content": "hi"}], max_tokens=1, gate={"gate_source": "probe"})
        except BharatCodeError:
            pass  # the failed attempt is already in the request log, which is what the fallback reads
