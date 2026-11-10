"""The benchmarks the improvement loop (improve.py) scores a change with. Each returns a Score:
one number, higher is better (0 to 1 for most), and a row per task so a candidate and the current
version can be compared task by task.

Three replay recorded material and need nothing live, so they also run as ordinary tests:
  verify-refs      references from real /deep-research runs, each labelled keep, drop or
                   unchecked by a person, with the registries' recorded answers
  collect-sources  the scouts' notes files from real runs, with the sources a person grouped
  router           start times of real Desktop replies, replayed through /auto's routing rule

Five run a command in BharatCode Desktop on a small project made for it (benchmarks/<target>/),
so they need Desktop and run only from scripts/improve.py:
  code-review, security-review   planted bugs and harmless changes; did the review find the bug,
                                  and stay quiet about the harmless change?
  simplify                        files with clear clean-ups and files already clean; did the
                                  change land, keep the hidden tests passing, and shorten the file?
  auto-handoff                    messages to /auto; was the handoff tool the agent's first call?
  deep-research                   one question of each kind; references kept, claims the checker
                                  marked, checklist items covered
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import evalpack, landing, refs, stats
from .scholar import Paper, ScholarError

ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "benchmarks"
PACKAGE_DIR = Path(__file__).resolve().parent
HEAVY, STANDARD = evalpack.HEAVY, evalpack.STANDARD


@dataclass
class Score:
    value: float
    tasks: list[dict] = field(default_factory=list)  # {"id", "score", ...detail}
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Score:
        return cls(float(data["value"]), list(data.get("tasks") or []), dict(data.get("detail") or {}))


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def bench_dir(target: str) -> Path:
    folder = BENCH_DIR / target
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is missing: run this from a spare-gpu checkout")
    return folder


# --- verify-refs ------------------------------------------------------------------------------------

VERIFY_SETTINGS = {"title_match": "TITLE_MATCH", "title_exact": "TITLE_EXACT", "title_shortened": "TITLE_SHORTENED"}


@contextlib.contextmanager
def _patched(module, values: dict):
    """Set module attributes for the duration (a candidate's settings), then put them back."""
    old = {name: getattr(module, name) for name in values}
    try:
        for name, value in values.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in old.items():
            setattr(module, name, value)


def _replay_http(page: dict | None) -> httpx.Client:
    def handle(request):
        if not page:
            return httpx.Response(404, text="not recorded")
        if page.get("error"):
            raise httpx.ConnectError(page["error"])
        return httpx.Response(int(page.get("status", 200)), text=page.get("text", ""))
    return httpx.Client(transport=httpx.MockTransport(handle))


def replay_verify(entry: dict) -> refs.Verdict:
    """verify_refs on one recorded reference: the registries' answer is the recorded one."""
    recorded = entry.get("recorded") or {}
    ref = refs.Reference.from_dict(entry["ref"], 1)

    def resolve(_ref, _http):
        if recorded.get("error"):
            raise ScholarError(recorded["error"])
        paper = recorded.get("paper")
        return Paper(**paper) if paper else None

    with _patched(refs, {"resolve": resolve}), _replay_http(recorded.get("page")) as http:
        return refs.verify(ref, http)


def verify_refs(settings: dict | None = None, data: Path | None = None) -> Score:
    """Share of recorded references verify_refs judges as a person did (keep, drop, unchecked)."""
    if data is None:  # the real references, and made-up bad ones built from their recorded answers
        folder = bench_dir("verify-refs")
        entries = [e for name in ("references.json", "synthetic.json") if (folder / name).is_file()
                   for e in json.loads((folder / name).read_text(encoding="utf-8"))]
    else:
        entries = json.loads(Path(data).read_text(encoding="utf-8"))
    values = {VERIFY_SETTINGS[k]: float(v) for k, v in (settings or {}).items() if k in VERIFY_SETTINGS}
    rows = []
    with _patched(refs, values):
        for entry in entries:
            verdict = replay_verify(entry)
            rows.append({"id": entry["ref"]["key"], "score": 1.0 if verdict.status == entry["label"] else 0.0,
                         "label": entry["label"], "got": verdict.status, "reason": verdict.reason})
    wrong = [r for r in rows if not r["score"]]
    return Score(_mean([r["score"] for r in rows]), rows, {
        "references": len(rows),
        "real_dropped": sum(1 for r in wrong if r["label"] == "keep" and r["got"] == "drop"),
        "bad_kept": sum(1 for r in wrong if r["label"] == "drop" and r["got"] == "keep"),
        "other_wrong": sum(1 for r in wrong if not (r["label"], r["got"]) in (("keep", "drop"), ("drop", "keep")))})


# --- collect-sources -------------------------------------------------------------------------------

def _pairs(groups: list[list[str]]) -> set[frozenset]:
    return {frozenset((a, b)) for group in groups for i, a in enumerate(group) for b in group[i + 1:]}


def collect_sources(data: Path | None = None) -> Score:
    """Per recorded run: the share of the scouts' entries collect_sources reads, and how well it
    groups them (pairwise F1 against a person's grouping). The notes are copied first:
    collect_sources may rename keys in them."""
    folder = data or bench_dir("collect-sources")
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    rows = []
    for run, want in expected.items():
        if run.startswith("_"):  # a note for people
            continue
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(folder / "runs" / run / "notes", Path(tmp) / "notes")
            path, _ = refs.collect_sources(Path(tmp))
            got = [[i["key"], *i.get("aliases", [])] for i in refs.load_sources(path)]
        read = sum(len(g) for g in got)
        want_pairs, got_pairs = _pairs(want["groups"]), _pairs(got)
        hit = len(want_pairs & got_pairs)
        recall = hit / len(want_pairs) if want_pairs else 1.0
        prec = hit / len(got_pairs) if got_pairs else (1.0 if not want_pairs else 0.0)
        f1 = (2 * prec * recall / (prec + recall)) if prec + recall else 0.0
        read_share = min(1.0, read / want["entries"]) if want["entries"] else 1.0
        rows.append({"id": run, "score": 0.5 * read_share + 0.5 * f1, "entries_read": read, "entries": want["entries"],
                     "sources": len(got), "expected_sources": len(want["groups"]), "merge_f1": round(f1, 3),
                     "missed_merges": sorted(sorted(p) for p in want_pairs - got_pairs)[:10],
                     "wrong_merges": sorted(sorted(p) for p in got_pairs - want_pairs)[:10]})
    return Score(_mean([r["score"] for r in rows]), rows, {"runs": len(rows)})


# --- router ----------------------------------------------------------------------------------------

ROUTER_DEFAULTS = {"min_saving": 20.0, "back_votes": 2, "start_window": 30.0, "start_keep": 5, "start_min": 2}
SWITCH_COST = 10.0  # seconds a move to the other model is charged: it may answer less well
HORIZON = 120.0  # seconds after a decision in which the chosen model's next reply shows its wait


def _next_wait(replies: list[tuple[str, float, float]], model: str, at: float) -> float | None:
    for name, created, seconds in replies:
        if created >= at and name == model:
            return seconds if created - at <= HORIZON else None
    return None


def router_replay(settings: dict | None = None, data: Path | None = None, preferred: str = HEAVY) -> Score:
    """/auto's measured-start rule replayed over recorded replies. At each reply's creation the rule
    picks a model from the replies already finished in the window; the wait that pick would have
    had is the next reply of that model within HORIZON seconds. Score: seconds saved per decision
    against always staying on the preferred model, with SWITCH_COST charged per move, divided by
    the mean wait (so 0.1 means a tenth of the waiting saved). Decisions where either model has no
    next reply in the horizon are skipped: their outcome isn't known."""
    p = {**ROUTER_DEFAULTS, **(settings or {})}
    doc = json.loads((data or bench_dir("router") / "starts.json").read_text(encoding="utf-8"))
    replies = sorted(((m, float(c), float(s)) for m, c, s in doc["replies"]), key=lambda r: r[1])
    models = sorted({m for m, _, _ in replies})
    other = next((m for m in models if m != preferred), None)
    away, votes = False, 0
    saved, waits_pref, moves, decisions = [], [], 0, 0
    for i, (_, at, _) in enumerate(replies):
        window = [r for r in replies[:i] if r[1] + r[2] <= at and r[1] >= at - p["start_window"] * 60]
        mine = stats.summarise_starts(window, keep=int(p["start_keep"]), minimum=int(p["start_min"]))
        pick = preferred
        if other and preferred in mine and other in mine:
            saving = mine[preferred] - mine[other]
            if saving >= p["min_saving"]:
                pick, away, votes = other, True, 0
            elif away:
                votes += 1
                if votes < int(p["back_votes"]):
                    pick = other
                else:
                    away, votes = False, 0
        stay, chosen = _next_wait(replies[i + 1:], preferred, at), _next_wait(replies[i + 1:], pick, at)
        if stay is None or chosen is None:
            continue
        decisions += 1
        cost = SWITCH_COST if pick != preferred else 0.0
        moves += pick != preferred
        saved.append(stay - chosen - cost)
        waits_pref.append(stay)
    mean_wait = _mean(waits_pref)
    value = (_mean(saved) / mean_wait) if mean_wait else 0.0
    rows = [{"id": "replay", "score": value, "decisions": decisions, "moves": moves,
             "mean_saved_seconds": round(_mean(saved), 2), "mean_preferred_wait": round(mean_wait, 2)}]
    return Score(value, rows, {"settings": p, "replies": len(replies), "decisions": decisions, "moves": moves})


# --- live: shared ----------------------------------------------------------------------------------

def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), "-c", "user.name=spare-gpu benchmark", "-c", "user.email=bench@localhost",
                    *args], check=True, capture_output=True)


def workspace(project: Path | None, scratch: Path) -> Path:
    """A fresh git repository holding a copy of the benchmark's project, committed, so each task
    starts from the same state and the user's own projects are never touched."""
    ws = Path(tempfile.mkdtemp(prefix="bench-", dir=scratch))
    if project is not None:
        shutil.copytree(project, ws, dirs_exist_ok=True)
    _git(ws, "init", "-q")
    (ws / ".gitignore").write_text(".gtu/\nspare-gpu/\n__pycache__/\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "benchmark baseline")
    return ws


def plant(ws: Path, seed: dict) -> None:
    """Make a seed's change (a find-and-replace in one file) as an uncommitted change."""
    path = ws / seed["file"]
    text = path.read_text(encoding="utf-8")
    if text.count(seed["find"]) != 1:
        raise ValueError(f"seed {seed['id']}: the text to change is in {seed['file']} {text.count(seed['find'])} times")
    path.write_text(text.replace(seed["find"], seed["replace"]), encoding="utf-8")


def _run(ws: Path, lead: str, prompt: str, agent, timeout: float) -> evalpack.RunResult:
    return evalpack.run_command(ws, lead, prompt, timeout=timeout, agent=agent)


def _cost(run: evalpack.RunResult, ws: Path) -> dict:
    return {"minutes": round(run.seconds / 60, 1), "tokens_by_model": evalpack.tokens_by_model(run, ws),
            "error": run.error}


# --- live: code-review and security-review -----------------------------------------------------------

REVIEWS = {"code-review": ("review-lead", "medium"), "security-review": ("security-lead", "")}


def review(target: str, agent=None, scratch: Path | None = None, only: list[str] | None = None,
           timeout: float = 3600.0) -> Score:
    """Each seed planted in a fresh copy of the project and reviewed. A planted bug scores 1 when a
    finding names its file within 3 lines; a harmless change scores 1 with no findings, half a
    point off per finding."""
    folder = bench_dir(target)
    lead, args = REVIEWS[target]
    seeds = [s for s in json.loads((folder / "seeds.json").read_text(encoding="utf-8")) if not only or s["id"] in only]
    scratch = scratch or Path(tempfile.mkdtemp(prefix=f"{target}-"))
    rows = []
    for seed in seeds:
        ws = workspace(folder / "project", scratch)
        plant(ws, seed)
        run = _run(ws, lead, evalpack.command_prompt(target, args), agent, timeout)
        found = evalpack.findings_from_text(run.text)
        if seed.get("clean"):
            score = max(0.0, 1.0 - 0.5 * len(found))
        else:
            hits, _ = evalpack.match_seeds(found, [seed])
            score = 1.0 if hits else 0.0
        rows.append({"id": seed["id"], "score": score, "clean": bool(seed.get("clean")), "what": seed.get("what"),
                     "findings": sorted(f"{f}:{n}" for f, n in found), **_cost(run, ws)})
    planted = [r for r in rows if not r["clean"]]
    return Score(_mean([r["score"] for r in rows]), rows, {
        "recall": _mean([r["score"] for r in planted]) if planted else None,
        "findings_on_harmless": sum(len(r["findings"]) for r in rows if r["clean"])})


# --- live: simplify -----------------------------------------------------------------------------------

def _wait_for_jobs(ws: Path, since: float, timeout: float, sleep=time.sleep) -> list:
    from .store import FINAL_STATUSES, Store
    path = ws / ".gtu" / "gtu.db"
    deadline = time.monotonic() + timeout
    while True:
        jobs = []
        if path.is_file():
            with Store(path) as store:
                jobs = [j for j in store.list_jobs() if _epoch(j.created_at) >= since - 5]
        # the lead hands its clean-ups to a fix job before it finishes, so no job by now means none
        if not jobs or all(j.status in FINAL_STATUSES for j in jobs) or time.monotonic() > deadline:
            return jobs
        sleep(10)


def _epoch(stamp) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _tests_pass(ws: Path, tests: Path) -> bool:
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests), "-q"],
                          cwd=ws, capture_output=True, text=True, timeout=600,
                          env={**__import__("os").environ, "PYTHONPATH": str(ws)})
    return proc.returncode == 0


def simplify(agent=None, scratch: Path | None = None, only: list[str] | None = None, timeout: float = 3600.0,
             sleep=time.sleep) -> Score:
    """Each file simplified in a fresh copy. The fix job's result, when there is one, is applied
    and the hidden tests (never shown to the job) run on it. A file with clean-ups scores 1 when the
    change lands, the hidden tests pass and the file got shorter; 0.5 when nothing lands; 0 when the
    hidden tests fail. A file that is already clean scores 1 when nothing lands."""
    folder = bench_dir("simplify")
    tasks = [t for t in json.loads((folder / "tasks.json").read_text(encoding="utf-8")) if not only or t["id"] in only]
    scratch = scratch or Path(tempfile.mkdtemp(prefix="simplify-"))
    rows = []
    for task in tasks:
        ws = workspace(folder / "project", scratch)
        before = len((ws / task["file"]).read_text(encoding="utf-8").splitlines())
        started = time.time()
        run = _run(ws, "simplify-lead", evalpack.command_prompt("simplify", task["file"]), agent, timeout)
        jobs = _wait_for_jobs(ws, started, timeout, sleep)
        landed = [j for j in jobs if j.status == "done" and j.output_dir]
        applied = False
        for job in landed:
            outcome = landing.apply(ws, Path(job.output_dir).name, force=True)
            applied = applied or outcome.counts.get("applied", 0) > 0
        after = len((ws / task["file"]).read_text(encoding="utf-8").splitlines())
        passed = _tests_pass(ws, folder / "hidden_tests")
        if not passed:
            score = 0.0
        elif task.get("clean"):
            score = 1.0 if not applied else 0.5
        else:
            score = 1.0 if applied and after < before else 0.5
        rows.append({"id": task["id"], "score": score, "clean": bool(task.get("clean")), "applied": applied,
                     "lines_before": before, "lines_after": after, "hidden_tests_pass": passed,
                     "jobs": [j.status for j in jobs], **_cost(run, ws)})
    return Score(_mean([r["score"] for r in rows]), rows, {})


# --- live: auto-handoff --------------------------------------------------------------------------------

def auto_handoff(agent=None, scratch: Path | None = None, only: list[str] | None = None,
                 timeout: float = 300.0) -> Score:
    """Each message sent the way /auto sends it, to the small handoff agent, with spare-gpu's tools
    denied (so nothing runs). Scores 1 when the agent's first tool call is the one expected
    (spare_auto, or spare_waits for an empty message), and records how long the turn took."""
    folder = bench_dir("auto-handoff")
    messages = [m for m in json.loads((folder / "messages.json").read_text(encoding="utf-8")) if not only or m["id"] in only]
    scratch = scratch or Path(tempfile.mkdtemp(prefix="auto-"))
    ws = workspace(None, scratch)
    rows = []
    for message in messages:
        prompt = evalpack.command_prompt("auto", message["text"], source=PACKAGE_DIR)
        began = time.monotonic()
        try:
            from . import desktop
            runner = agent or desktop.DesktopAgent(desktop.discover(), timeout=timeout)
            result = runner.run_agent(ws, "spare-auto", prompt, model=f"bharatcode/{STANDARD}",  # as /auto runs it
                                      permission=[{"permission": "spare-gpu_*", "pattern": "*", "action": "deny"}])
            tools, error = [t.split(" ")[0] for t in result.tools], result.error
        except Exception as exc:  # Desktop missing or the run failed: the message scores 0
            tools, error = [], str(exc)
        first = tools[0] if tools else None
        rows.append({"id": message["id"], "score": 1.0 if first == f"spare-gpu_{message['expect']}" else 0.0,
                     "first_tool": first, "expected": message["expect"], "seconds": round(time.monotonic() - began, 1),
                     "error": error})
    return Score(_mean([r["score"] for r in rows]), rows,
                 {"median_seconds": statistics.median([r["seconds"] for r in rows]) if rows else None})


# --- live: deep-research ---------------------------------------------------------------------------------

def review_figures(folder: Path) -> dict:
    """From a run's review.md: claims checked and marked, checklist items covered and in all."""
    text = (folder / "review.md").read_text(encoding="utf-8", errors="replace") if (folder / "review.md").is_file() else ""
    checked = re.search(r"(\d+)\s+(?:distinct\s+)?(?:report\s+)?claims?(?:\s+clusters?)?\s+checked|checked[^\d\n]{0,20}(\d+)", text, re.I)
    marked = re.search(r"marked[^\d\n]{0,5}(\d+)|(\d+)\s+marked", text, re.I)
    covered = re.search(r"(\d+)\s*(?:of|/)\s*(\d+)\s+(?:checklist\s+)?items?\s+covered|covered[^\d\n]{0,12}(\d+)\s*(?:of|/)\s*(\d+)", text, re.I)
    number = lambda m: next((int(g) for g in m.groups() if g), None) if m else None  # noqa: E731
    out = {"claims_checked": number(checked), "claims_marked": number(marked), "items_covered": None, "items": None}
    if covered:
        nums = [int(g) for g in covered.groups() if g]
        out["items_covered"], out["items"] = nums[0], nums[1]
    return out


def deep_research(agent=None, scratch: Path | None = None, only: list[str] | None = None,
                  timeout: float = 5400.0) -> Score:
    """Each question researched in an empty project. Per question: 0.4 x the share of checked
    references kept, 0.3 x the share of claims the checker left unmarked, 0.3 x the share of
    checklist items covered; a run that leaves no report scores 0."""
    folder = bench_dir("deep-research")
    tasks = [t for t in json.loads((folder / "tasks.json").read_text(encoding="utf-8")) if not only or t["id"] in only]
    scratch = scratch or Path(tempfile.mkdtemp(prefix="research-"))
    rows = []
    for task in tasks:
        ws = workspace(None, scratch)
        run = _run(ws, "research-lead", evalpack.command_prompt("deep-research", task["question"]), agent, timeout)
        out = evalpack.latest_folder(ws)
        tally = evalpack.research_tally(out, task.get("key_papers", [])) if out else {}
        figures = review_figures(out) if out else {}
        if not out or not (out / "report.md").is_file():
            score = 0.0
        else:
            kept = tally.get("resolved_share")
            checked, marked = figures.get("claims_checked"), figures.get("claims_marked")
            unmarked = 1 - marked / checked if checked and marked is not None else 0.0
            coverage = figures["items_covered"] / figures["items"] if figures.get("items") else 0.0
            score = 0.4 * (kept if kept is not None else 0.0) + 0.3 * max(0.0, unmarked) + 0.3 * coverage
        rows.append({"id": task["id"], "score": round(score, 4), "kind": task.get("kind"), **tally, **figures,
                     **_cost(run, ws)})
    return Score(_mean([r["score"] for r in rows]), rows, {})


LIVE = {"code-review": lambda **kw: review("code-review", **kw), "security-review": lambda **kw: review("security-review", **kw),
        "simplify": simplify, "auto-handoff": auto_handoff, "deep-research": deep_research}
