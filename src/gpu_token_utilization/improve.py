"""The improvement loop: propose a change to one part of spare-gpu, score it against the current
version on a fixed benchmark (bench.py), and keep it only when it wins by a clear margin and the
person running the loop says so. Every kept change is archived first, so it can be rolled back.

A target is one of three kinds:
  params   settings in tuning.json (verify-refs, router). A proposal is a search over the
           target's grid; scoring is a replay, so it takes seconds.
  prompts  agent prompts (code-review, security-review, simplify, auto-handoff, deep-research).
           A proposal is the heavy model rewriting the prompts from the failures of the current
           version's last run. Desktop reads prompts when it starts, so a candidate is installed,
           Desktop restarted, and only then evaluated; the loop checks what is installed.
  code     code with a benchmark (collect-sources). Nothing is proposed: the benchmark's score is
           the bar a code change must not fall below (`bar`), and a test holds it.

Nothing here commits, pushes or runs on its own: scripts/improve.py runs each step on request,
and promotion needs the step's own command. Records live in research/improve/<target>/:
runs/ (every evaluation), candidates/ (proposals), archive/ (what a promotion replaced), bar.json,
and research/improve/history.jsonl (every promotion and rollback).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import bench, evalpack, tuning
from .bench import Score

ACCEPT_MARGIN = 0.05  # a candidate must beat the current version by 5% of its score ...
ROOM_SHARE = 0.2  # ... or by a fifth of the room left to a perfect 1.0, whichever is smaller
MIN_GAIN = 0.005
IMPROVE_DIR = bench.ROOT / "research" / "improve"
PACKAGE_DIR = bench.PACKAGE_DIR
DENY_ALL = [{"permission": "*", "pattern": "*", "action": "deny"}]


class ImproveError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    name: str
    kind: str  # params | prompts | code
    about: str
    section: str | None = None  # params: tuning.json's section
    grid: dict = field(default_factory=dict)  # params: the values the search tries per setting
    files: tuple[str, ...] = ()  # prompts: paths under the package (pack/agents/x.md, agents/x.md)

    @property
    def live(self) -> bool:
        return self.kind == "prompts"


TARGETS = {t.name: t for t in (
    Target("verify-refs", "params", "verify_refs's thresholds, scored on references a person labelled",
           section="verify_refs", grid={"title_match": [0.8, 0.85, 0.9], "title_exact": [0.9, 0.95, 0.98],
                                        "title_shortened": [0.65, 0.7, 0.75, 0.8]}),
    Target("router", "params", "/auto's routing figures, scored by replaying real reply start times",
           section="router", grid={"min_saving": [10.0, 15.0, 20.0, 30.0, 45.0], "back_votes": [1, 2, 3],
                                   "start_window": [15.0, 30.0, 60.0], "start_keep": [3, 5, 8], "start_min": [2, 3]}),
    Target("collect-sources", "code", "collect_sources, scored on real scouts' notes a person grouped"),
    Target("code-review", "prompts", "/code-review's agents, scored on planted bugs and harmless changes",
           files=("pack/agents/review-lead.md", "pack/agents/review-finder.md", "pack/agents/review-verifier.md")),
    Target("security-review", "prompts", "/security-review's agents, scored on planted vulnerabilities and bait",
           files=("pack/agents/security-lead.md", "pack/agents/security-finder.md", "pack/agents/security-verifier.md")),
    Target("simplify", "prompts", "/simplify's agents, scored on files with clean-ups and files already clean",
           files=("pack/agents/simplify-lead.md", "pack/agents/simplify-finder.md")),
    Target("auto-handoff", "prompts", "/auto's handoff agent, scored on whether its first call is the handoff",
           files=("agents/spare-auto.md",)),
    Target("deep-research", "prompts", "/deep-research's agents, scored on one question of each kind",
           files=("pack/agents/research-lead.md", "pack/agents/research-scout.md",
                  "pack/agents/research-writer.md", "pack/agents/research-checker.md")),
)}


def target(name: str) -> Target:
    if name not in TARGETS:
        raise ImproveError(f"unknown target {name!r}; the targets are {', '.join(TARGETS)}")
    return TARGETS[name]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def folder(t: Target, base: Path | None = None) -> Path:
    return (base or IMPROVE_DIR) / t.name


def _write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _history(base: Path | None, entry: dict) -> None:
    path = (base or IMPROVE_DIR) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": _now(), **entry}, ensure_ascii=False) + "\n")


def history(base: Path | None = None, name: str | None = None) -> list[dict]:
    path = (base or IMPROVE_DIR) / "history.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if name is None or r.get("target") == name]


# --- candidates --------------------------------------------------------------------------------------

def save_candidate(t: Target, *, params: dict | None = None, files: dict[str, str] | None = None, note: str = "",
                   base: Path | None = None) -> str:
    """Store a proposal: settings for a params target, full file texts for a prompts target."""
    cid = _now()
    where = folder(t, base) / "candidates" / cid
    for rel, text in (files or {}).items():
        dest = where / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    _write_json(where / "candidate.json", {"id": cid, "target": t.name, "params": params, "files": sorted(files or {}),
                                           "note": note})
    return cid


def load_candidate(t: Target, cid: str, base: Path | None = None) -> dict:
    where = folder(t, base) / "candidates" / cid
    if not (where / "candidate.json").is_file():
        raise ImproveError(f"no candidate {cid} for {t.name}")
    doc = json.loads((where / "candidate.json").read_text(encoding="utf-8"))
    doc["texts"] = {rel: (where / "files" / rel).read_text(encoding="utf-8") for rel in doc.get("files") or []}
    return doc


def current_texts(t: Target, package: Path | None = None) -> dict[str, str]:
    return {rel: ((package or PACKAGE_DIR) / rel).read_text(encoding="utf-8") for rel in t.files}


def current_settings(t: Target) -> dict:
    defaults = {"verify-refs": {k: getattr(bench.refs, v) for k, v in bench.VERIFY_SETTINGS.items()},
                "router": dict(bench.ROUTER_DEFAULTS)}.get(t.name, {})
    return {**defaults, **(tuning.load().get(t.section) or {})} if t.section else {}


# --- scoring -----------------------------------------------------------------------------------------

def score_params(t: Target, settings: dict) -> Score:
    return bench.verify_refs(settings) if t.name == "verify-refs" else bench.router_replay(settings)


def evaluate(name: str, candidate: str | None = None, base: Path | None = None, **live) -> tuple[Score, Path]:
    """Score the current version (or a candidate) and record the run. For a prompts target the
    installed prompts must be the ones asked for: see install()."""
    t = target(name)
    label = candidate or "current"
    if t.kind == "params":
        settings = {**current_settings(t), **((load_candidate(t, candidate, base)["params"] or {}) if candidate else {})}
        score = score_params(t, settings)
    elif t.kind == "code":
        if candidate:
            raise ImproveError(f"{t.name} is code: change the code, then evaluate the current version")
        score = bench.collect_sources()
    else:
        expected = load_candidate(t, candidate, base)["texts"] if candidate else current_texts(t)
        problems = installed_differences(t, expected)
        if problems:
            raise ImproveError(f"Desktop doesn't have the {label} prompts installed ({'; '.join(problems[:3])}). Run "
                               f"`python scripts/improve.py {t.name} install{' --candidate ' + candidate if candidate else ''}`, "
                               "fully restart BharatCode Desktop, then evaluate again.")
        score = bench.LIVE[t.name](**live)
    path = _write_json(folder(t, base) / "runs" / f"{_now()}_{label}.json",
                       {"target": t.name, "label": label, **score.to_dict()})
    return score, path


def runs(name: str, base: Path | None = None, label: str | None = None) -> list[dict]:
    where = folder(target(name), base) / "runs"
    found = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(where.glob("*.json"))] if where.is_dir() else []
    return [r for r in found if label is None or r.get("label") == label]


def compare(current: Score, candidate: Score) -> dict:
    """Paired by task: the candidate is better when its score beats the current one by the margin
    (ACCEPT_MARGIN of the current score, or ROOM_SHARE of the room left below 1.0 when that is
    smaller, and at least MIN_GAIN) and the bootstrap's lower bound of the per-task differences
    isn't below zero (no sign that it is worse on the tasks)."""
    mine = {t["id"]: t["score"] for t in current.tasks}
    theirs = {t["id"]: t["score"] for t in candidate.tasks}
    common = [k for k in mine if k in theirs]
    diffs = [theirs[k] - mine[k] for k in common]
    gain = candidate.value - current.value
    room = 1.0 - current.value if 0.0 <= current.value < 1.0 else float("inf")
    needed = max(MIN_GAIN, min(ACCEPT_MARGIN * abs(current.value), ROOM_SHARE * room))
    lower = evalpack.bootstrap_lower(diffs) if len(diffs) >= 2 else (diffs[0] if diffs else gain)
    worse = [k for k in common if theirs[k] < mine[k]]
    better = gain >= needed and lower >= 0
    return {"verdict": "better" if better else "not better", "current": current.value, "candidate": candidate.value,
            "gain": gain, "needed": needed, "bootstrap_lower": lower, "tasks_compared": len(common),
            "tasks_worse": worse, "tasks_better": [k for k in common if theirs[k] > mine[k]]}


def decide(name: str, candidate: str, base: Path | None = None) -> dict:
    """Compare a candidate's latest run with the current version's latest run, and record it on the candidate."""
    t = target(name)
    mine, theirs = runs(name, base, "current"), runs(name, base, candidate)
    if not mine or not theirs:
        raise ImproveError(f"evaluate both first: the current version ({len(mine)} run(s)) and {candidate} ({len(theirs)})")
    result = compare(Score.from_dict(mine[-1]), Score.from_dict(theirs[-1]))
    path = folder(t, base) / "candidates" / candidate / "candidate.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    _write_json(path, {**doc, "decision": result})
    return result


# --- proposing ---------------------------------------------------------------------------------------

def search_params(name: str, rounds: int = 2, base: Path | None = None) -> tuple[str | None, dict]:
    """Coordinate search over the target's grid: each setting in turn tries its values with the
    others held at the best so far, twice over. A candidate is saved only when the best beats the
    current settings (compare's rule); returns its id (or None) and the comparison."""
    t = target(name)
    if t.kind != "params":
        raise ImproveError(f"{name} has no settings to search")
    now = current_settings(t)
    baseline = score_params(t, now)
    best, best_score = dict(now), baseline
    tried: dict[tuple, Score] = {}
    for _ in range(rounds):
        for key, values in t.grid.items():
            for value in values:
                trial = {**best, key: value}
                sig = tuple(sorted(trial.items()))
                if sig not in tried:
                    tried[sig] = score_params(t, trial)
                if tried[sig].value > best_score.value:
                    best, best_score = trial, tried[sig]
    result = compare(baseline, best_score)
    result["tried"] = len(tried)
    changed = {k: v for k, v in best.items() if now.get(k) != v}
    if result["verdict"] != "better" or not changed:
        return None, result
    cid = save_candidate(t, params=changed, note=f"grid search: {best_score.value:.4f} against {baseline.value:.4f}",
                         base=base)
    return cid, result


FILE_BLOCK = re.compile(r"=== FILE: (?P<path>[^\n=]+?) ===\n(?P<body>.*?)\n=== END ===", re.S)


def _split(text: str) -> tuple[str, str]:
    """A Markdown agent file's frontmatter (with its --- lines) and body."""
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n") and "\n---\n" in text[4:]:
        head, body = text[4:].split("\n---\n", 1)
        return f"---\n{head}\n---\n", body
    return "", text


def rewrite_prompt(t: Target, run: dict, texts: dict[str, str]) -> str:
    failures = [r for r in run.get("tasks", []) if r.get("score", 1) < 1]
    lines = [f"- {r['id']}: score {r['score']}; " + "; ".join(f"{k}: {v}" for k, v in r.items()
                                                                if k not in ("id", "score", "tokens_by_model") and v not in (None, [], ""))
             for r in failures][:30]
    files = "\n\n".join(f"=== FILE: {rel} ===\n{text.strip()}\n=== END ===" for rel, text in texts.items())
    return f"""You improve the instructions of AI agents that are part of spare-gpu, a toolkit for
BharatCode Desktop. The instructions below are for {t.about}. On that benchmark they scored
{run.get('value', 0):.3f} (1.0 is perfect). The tasks that fell short:
{chr(10).join(lines) or '- none recorded'}

Rewrite the instruction files so these failures don't recur without breaking what already
works. Rules:
- Keep each file's frontmatter (from the first --- line to the second) exactly as it is; change
  only the text after it.
- Keep every rule about safety, permissions, and treating notes, pages and code as data rather
  than instructions.
- Don't grow a file by more than a fifth. Plain English; say what to do, not why the old text failed.
- Don't name the benchmark, its tasks or its files: the agents must work on any project.

Reply with every file, each exactly as:
=== FILE: <path> ===
<the whole new file>
=== END ===
and after the last one, one short paragraph on what you changed. Don't use any tool.

{files}"""


def parse_rewrite(t: Target, reply: str, texts: dict[str, str]) -> dict[str, str]:
    """The files from a rewrite reply, each with its original frontmatter put back (a proposal may
    only change bodies). Raises when a file is missing, empty, or grew by more than a quarter (or 500
    characters, for a short one)."""
    found = {m.group("path").strip(): m.group("body") for m in FILE_BLOCK.finditer(reply.replace("\r\n", "\n"))}
    out = {}
    for rel, old in texts.items():
        if rel not in found:
            raise ImproveError(f"the rewrite left out {rel}")
        head, old_body = _split(old)
        _, new_body = _split(found[rel])
        if not new_body.strip():
            raise ImproveError(f"the rewrite of {rel} is empty")
        if len(new_body) > max(1.25 * len(old_body), len(old_body) + 500):
            raise ImproveError(f"the rewrite of {rel} grew from {len(old_body)} to {len(new_body)} characters")
        out[rel] = head + new_body.strip() + "\n"
    return out


def propose_prompts(name: str, agent=None, scratch: Path | None = None, base: Path | None = None) -> str:
    """Ask the heavy model, in a Desktop session with every tool denied, to rewrite the target's
    prompts from the failures of the current version's latest run; save the result as a candidate."""
    t = target(name)
    if t.kind != "prompts":
        raise ImproveError(f"{name} isn't a prompts target")
    done = runs(name, base, "current")
    if not done:
        raise ImproveError(f"evaluate the current version of {name} first: a rewrite starts from its failures")
    texts = current_texts(t)
    from . import desktop
    agent = agent or desktop.DesktopAgent(desktop.discover(), timeout=1800.0)
    work = scratch or bench.workspace(None, Path(__import__("tempfile").mkdtemp(prefix="improve-")))
    result = agent.run_agent(work, None, rewrite_prompt(t, done[-1], texts), model=f"bharatcode/{bench.HEAVY}",
                             permission=DENY_ALL)
    if result.error and not result.text:
        raise ImproveError(f"the rewrite failed: {result.error}")
    files = parse_rewrite(t, result.text, texts)
    note = result.text.rsplit("=== END ===", 1)[-1].strip()[:1500]
    return save_candidate(t, files=files, note=note, base=base)


# --- installing prompts into Desktop -------------------------------------------------------------------

def _filled(t: Target, texts: dict[str, str]) -> list[tuple[str, str, str]]:
    """(kind, name, text) as Desktop's config folder holds them: model ids filled in, and the
    standard-model variant for a switchable agent."""
    from . import pack
    models, _ = pack.resolve_models()
    out = []
    for rel, text in texts.items():
        name = Path(rel).stem
        if rel.startswith("pack/"):
            out.append(("agent", name, models.fill(text)))
            if pack.variant_of(name):
                out.append(("agent", pack.variant_of(name), models.fill(pack.variant_text(text))))
        else:
            out.append(("agent", name, text))
    return out


def _config_folders() -> list[Path]:
    from . import bc_config
    channels = bc_config.installed_channels()
    return [bc_config.channel_config_dir(c) for c in channels] or [bc_config.config_dir()]


def installed_differences(t: Target, texts: dict[str, str], folders: list[Path] | None = None) -> list[str]:
    problems = []
    for where in folders or _config_folders():
        for kind, name, text in _filled(t, texts):
            path = where / kind / f"{name}.md"
            if not path.is_file() or path.read_text(encoding="utf-8-sig") != text:
                problems.append(f"{path} differs")
    return problems


def install(name: str, candidate: str | None = None, base: Path | None = None, folders: list[Path] | None = None) -> list[str]:
    """Write a candidate's prompts (or the current ones) into Desktop's config folders. Desktop
    reads them at its next start."""
    from . import bc_config
    t = target(name)
    if t.kind != "prompts":
        raise ImproveError(f"{name} has no prompts to install; its settings apply at once")
    texts = load_candidate(t, candidate, base)["texts"] if candidate else current_texts(t)
    written = []
    for where in folders or _config_folders():
        outcome = bc_config.install_markdown(where, _filled(t, texts))
        written += [f"{where}: {k} {v}" for k, v in outcome.items()]
    return written


# --- promoting and rolling back ------------------------------------------------------------------------

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def promote(name: str, candidate: str, force: bool = False, base: Path | None = None,
            package: Path | None = None, tuning_file: Path | None = None) -> dict:
    """Make a candidate the current version, after archiving what it replaces. Needs the
    candidate's recorded decision to be "better" unless `force`."""
    t = target(name)
    doc = load_candidate(t, candidate, base)
    decision = doc.get("decision") or {}
    if decision.get("verdict") != "better" and not force:
        raise ImproveError(f"{candidate} isn't recorded as better than the current version "
                           f"({decision.get('verdict', 'not compared')}); run `decide` first, or pass --force")
    archive = folder(t, base) / "archive" / _now()
    if t.kind == "params":
        path = tuning_file or tuning.TUNING_FILE
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        _write_json(archive / "tuning.json", data)
        data.setdefault(t.section, {}).update(doc["params"] or {})
        _write_json(path, data)
        changed = sorted(doc["params"] or {})
    elif t.kind == "prompts":
        changed = []
        for rel, text in doc["texts"].items():
            dest = (package or PACKAGE_DIR) / rel
            (archive / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, archive / "files" / rel)
            dest.write_text(text, encoding="utf-8")
            changed.append(rel)
    else:
        raise ImproveError(f"{name} is code: there is nothing to promote")
    entry = {"action": "promote", "target": name, "candidate": candidate, "archive": str(archive), "changed": changed,
             "current": decision.get("current"), "score": decision.get("candidate"), "forced": force and
             decision.get("verdict") != "better"}
    _history(base, entry)
    return entry


def rollback(name: str, base: Path | None = None, package: Path | None = None, tuning_file: Path | None = None) -> dict:
    """Put back what the latest promotion of the target replaced."""
    t = target(name)
    events = history(base, name)
    undone = {e.get("archive") for e in events if e["action"] == "rollback"}
    last = next((e for e in reversed(events) if e["action"] == "promote" and e["archive"] not in undone), None)
    if last is None:
        raise ImproveError(f"nothing of {name} to roll back")
    archive = Path(last["archive"])
    if t.kind == "params":
        path = tuning_file or tuning.TUNING_FILE
        old = json.loads((archive / "tuning.json").read_text(encoding="utf-8"))
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data[t.section] = old.get(t.section, {})
        _write_json(path, data)
    else:
        for rel in last["changed"]:
            shutil.copy2(archive / "files" / rel, (package or PACKAGE_DIR) / rel)
    entry = {"action": "rollback", "target": name, "candidate": last["candidate"], "archive": last["archive"]}
    _history(base, entry)
    return entry


# --- the bar a code change must hold --------------------------------------------------------------------

def record_bar(name: str, base: Path | None = None) -> dict:
    """Store the current version's score as the bar later changes must not fall below."""
    t = target(name)
    if t.live:
        raise ImproveError(f"{name} runs live; its runs are its record")
    score = bench.collect_sources() if t.kind == "code" else score_params(t, current_settings(t))
    bar = {"target": name, "value": score.value, "at": _now(),
           "settings": current_settings(t) if t.kind == "params" else None}
    _write_json(folder(t, base) / "bar.json", bar)
    return bar


def bar(name: str, base: Path | None = None) -> dict | None:
    path = folder(target(name), base) / "bar.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def grid_size(t: Target) -> int:
    return len(list(itertools.product(*t.grid.values()))) if t.grid else 0
