"""Wait-aware model routing: run a job on whichever allowed model will start it sooner.

Moving work to a weaker model is only for task types measured as switch-safe (routing_policy.json,
written by scripts/eval_models.py): types where Qwen's results were shown no worse than
DeepSeek's. Moving to a stronger model never costs quality, so any type may move up when that
starts it sooner. The rules, in order:
- not switch-safe and no stronger model to move up to, or no wait data on the preferred model
  -> the preferred model;
- an alternative is chosen only if it saves at least GTU_ROUTE_MIN_SAVING seconds (default 20)
  on the estimate AND its lane has a free slot with nothing queued, so routed work never piles
  into a queue (herding would erase the gain);
- hysteresis: once routing away from a model, it takes two readings in a row where the preferred
  model is no longer slower by the threshold before routing back to it;
- sticky per job: the choice is stored on the job and every step and retry uses it; the one
  exception is the fallback, when a routed job's check keeps nothing, which reruns it once on
  the preferred model.
Routing happens inside spare-gpu (the model of the Desktop session it creates for the job):
BharatCode's apps only serve their own catalog, so a routing "model" can't be added to them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import waits
from .waits import Estimate, View

POLICY_FILE = Path(__file__).with_name("routing_policy.json")
POLICY_ENV = "GTU_ROUTING_POLICY"  # a different policy file
SWITCH_SAFE_ENV = "GTU_SWITCH_SAFE"  # "tests,docs": the user's own override of the policy
SWITCH_SAFE_FILE = "switch-safe"  # the same, per project, in .gtu/
PREFERRED_ENV = "GTU_PREFERRED_MODEL"
MIN_SAVING_ENV = "GTU_ROUTE_MIN_SAVING"
ROUTE_ENV = "GTU_ROUTE"  # "off" disables routing
# The model a delegated task would otherwise run on: spare_delegate is called from the user's chat,
# typically on the heavy model. Queued jobs default to the worker's own model (Qwen) instead.
DELEGATE_PREFERRED = "deepseek-v4.1-flash"
DEFAULT_MIN_SAVING = 20.0
BACK_VOTES = 2


def load_policy(folder: Path | None = None) -> dict:
    """The shipped (or GTU_ROUTING_POLICY) policy, with the user's own overrides on top: the
    project's .gtu/switch-safe file (one type per line; read on every decision, so no restart of
    BharatCode is needed) and GTU_SWITCH_SAFE."""
    path = Path(os.environ.get(POLICY_ENV) or POLICY_FILE)
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        policy = {}
    types = dict(policy.get("types") or {})
    chosen = [k.strip() for k in os.environ.get(SWITCH_SAFE_ENV, "").split(",")]
    if folder is not None:
        try:
            chosen += [line.strip() for line in (folder / SWITCH_SAFE_FILE).read_text(encoding="utf-8").splitlines()]
        except OSError:
            pass
        chosen += _settings(folder).get("switch_safe") or []
    for kind in filter(None, chosen):
        types[kind] = "switch-safe"
    return {**policy, "types": types}


def switch_safe(kind: str, policy: dict | None = None) -> bool:
    return (policy or load_policy())["types"].get(kind) == "switch-safe"


# BharatCode's refusal when the plan doesn't include a model, e.g. "Your BharatCode Lite plan includes
# Qwen 3.8 27B (qwen-3.8-27b). DeepSeek V4.1 Flash needs BharatCode Pro ..." (seen 4 Nov 2026).
PLAN_REFUSAL = re.compile(r"plan includes [^()]*\((?P<allowed>[\w.\-]+)\)[^.]*\..*?needs BharatCode", re.S)


def plan_refusal(error: str | None) -> str | None:
    """The model the plan does include, if `error` is BharatCode refusing a model for the plan."""
    m = PLAN_REFUSAL.search(error or "")
    return waits.model_id(m.group("allowed")) if m else None


def preferred_model(default: str, folder: Path | None = None) -> str:
    """The model the user would use without routing: GTU_PREFERRED_MODEL, else the project's
    settings file (`folder` is its .gtu), else `default`."""
    return waits.model_id(os.environ.get(PREFERRED_ENV) or (_settings(folder).get("preferred_model") if folder else None)
                          or default)


def _settings(folder: Path):
    """The project's settings, read leniently: a broken file is reported where jobs start, not here."""
    from . import settings

    try:
        return settings.load(Path(folder).parent)
    except ValueError:
        return settings.Settings(Path(folder) / settings.FILE)


def min_saving() -> float:
    try:
        return float(os.environ.get(MIN_SAVING_ENV) or DEFAULT_MIN_SAVING)
    except ValueError:
        return DEFAULT_MIN_SAVING


def enabled() -> bool:
    return os.environ.get(ROUTE_ENV, "").strip().lower() not in ("off", "0", "false", "no")


@dataclass
class State:
    """What the router remembers in the project's .gtu folder: hysteresis, and models the user's
    BharatCode plan refused (BharatCode's catalog lists every model; only a refusal tells)."""
    away_from: str | None = None  # the preferred model we last routed away from
    back_votes: int = 0
    unavailable: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, folder: Path) -> State:
        try:
            data = json.loads((folder / "router.json").read_text(encoding="utf-8"))
            return cls(data.get("away_from"), int(data.get("back_votes") or 0), list(data.get("unavailable") or []))
        except (OSError, ValueError, TypeError):
            return cls()

    def mark_unavailable(self, model: str) -> None:
        if waits.model_id(model) not in self.unavailable:
            self.unavailable.append(waits.model_id(model))

    def save(self, folder: Path) -> None:
        try:
            (folder / "router.json").write_text(json.dumps(self.__dict__), encoding="utf-8")
        except OSError:
            pass


@dataclass
class Choice:
    model: str
    preferred: str
    reason: str
    estimates: list[Estimate] = field(default_factory=list)

    @property
    def routed(self) -> bool:
        return self.model != self.preferred

    def summary(self) -> str:
        """One line for the user: which model, and why (with the estimates behind it)."""
        how = "; ".join(waits.explain(e) for e in self.estimates)
        return f"Runs on {waits.name(self.model)}: {self.reason}." + (f" Waits now: {how}." if how else "")


def stronger(model: str, than: str) -> bool:
    """Whether `model` is the stronger of the two. waits.models() lists the heavy model first;
    a model not listed counts as the weakest."""
    order = waits.models()

    def rank(m: str) -> int:
        m = waits.model_id(m)
        return order.index(m) if m in order else len(order)

    return rank(model) < rank(than)


def decide(store, folder: Path, kind: str, preferred: str, http_factory) -> Choice:
    """choose() with fresh wait figures and this project's policy overrides and router state."""
    state = State.load(folder)
    policy = load_policy(folder)
    can_move = switch_safe(kind, policy) or any(stronger(m, preferred) for m in waits.models())
    if not enabled() or not can_move:  # no need to fetch anything
        choice = choose(kind, preferred, [], state, policy)
    else:
        try:
            with http_factory() as http:
                current = waits.views(store, http)
        except Exception:  # noqa: BLE001 - routing is an optimisation: no figures means "don't switch", never a failed job
            current = []
        choice = choose(kind, preferred, current, state, policy)
    state.save(folder)
    return choice


def choose(kind: str, preferred: str, views: list[View], state: State | None = None,
           policy: dict | None = None, threshold: float | None = None) -> Choice:
    preferred = waits.model_id(preferred)
    state = state if state is not None else State()
    threshold = min_saving() if threshold is None else threshold
    views = [v for v in views if v.model not in state.unavailable]
    estimates = [waits.estimate(v) for v in views]
    by_model = {e.model: e for e in estimates}
    if preferred in state.unavailable:
        usable = [m for m in waits.models() if m not in state.unavailable]
        if usable:
            return Choice(usable[0], usable[0], f"your BharatCode plan doesn't include {waits.name(preferred)}",
                          estimates)
    if not enabled():
        return Choice(preferred, preferred, "routing is off (GTU_ROUTE)", estimates)
    # Moving to a weaker model needs the type measured as switch-safe; moving to a stronger one
    # never costs quality, so it is allowed whenever it starts the work sooner.
    safe = switch_safe(kind, policy)
    if not safe and not any(stronger(m, preferred) for m in waits.models()):
        return Choice(preferred, preferred,
                      f"{kind} jobs aren't marked switch-safe, so they stay on the preferred model", estimates)
    pref = by_model.get(preferred)
    if pref is None or pref.seconds is None:
        return Choice(preferred, preferred, "no wait data on the preferred model, so not switching", estimates)
    free = {v.model: v.free_slot for v in views}
    candidates = [e for e in estimates if e.model != preferred and e.seconds is not None and free.get(e.model)
                  and (safe or stronger(e.model, preferred))]
    best = min(candidates, key=lambda e: e.seconds, default=None)
    saving = pref.seconds - best.seconds if best else 0.0

    if best and saving >= threshold:
        state.away_from, state.back_votes = preferred, 0
        return Choice(best.model, preferred, f"saves about {waits._secs(saving)} of waiting over "
                      f"{waits.name(preferred)}, and its lane has a free slot", estimates)
    if state.away_from == preferred and best:
        state.back_votes += 1
        if state.back_votes < BACK_VOTES:
            return Choice(best.model, preferred, "still routed away (the preferred model was faster only "
                          f"{state.back_votes} reading in a row; {BACK_VOTES} are needed to switch back)", estimates)
    state.away_from, state.back_votes = None, 0
    if not any(e.model != preferred for e in estimates) and state.unavailable:
        why = "your BharatCode plan includes no other model"
    elif best is None and any(e.model != preferred for e in estimates):
        why = "no other model has a free slot right now"
    else:
        why = f"no model would save {waits._secs(threshold)} or more"
    return Choice(preferred, preferred, why, estimates)
