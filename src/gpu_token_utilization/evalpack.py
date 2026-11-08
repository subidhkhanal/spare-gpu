"""Benchmarks for the pack's commands (plan §8): run a command's lead as a BharatCode Desktop
session in each setup, tally the outcome from the files it leaves, read the tokens per model from
Desktop's own record of the replies, and decide which switchable roles are switch-safe with the
rule scripts/eval_models.py uses for job types. scripts/eval_research.py and scripts/eval_review.py
are thin wrappers; everything here runs without BharatCode in the tests (a fake Desktop server,
recorded files).

Desktop reads the pack's config when it starts, so a run covers one setup: install it
(install_args), restart Desktop, and run the script from Desktop's terminal.

A setup is how the pack is installed for the run:
  deepseek              every role on the heavy model (GTU_MODELS names it twice)
  split                 the default: heavy leads and verifiers, standard helpers
  lite                  every role on the standard model (--lite)
  split-standard-roles  the split, with the switchable roles told to use their -standard variant
"""

from __future__ import annotations

import json
import random
import re
import statistics
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from . import desktop, pack, scholar, stats
from .api import env_without_key

HEAVY, STANDARD = "deepseek-v4.1-flash", "qwen-3.8-27b"
SETUPS: dict[str, dict] = {
    "deepseek": {"env": {"GTU_MODELS": f"{HEAVY},{HEAVY}"}, "lite": False},
    "split": {"env": {}, "lite": False},
    "lite": {"env": {}, "lite": True},
    "split-standard-roles": {"env": {"GTU_SWITCH_SAFE": ",".join(pack.SWITCHABLE)}, "lite": False},
}
DELTA_SHARE = 0.05  # non-inferiority margin: 5% of the heavy model's mean score
BOOTSTRAP = 2000
FINDING = re.compile(r"`?([\w./\\-]+\.\w+):(\d+)`?")
COMMAND_LEADS = {"deep-research": "research-lead", "code-review": "review-lead", "security-review": "security-lead",
                 "simplify": "simplify-lead"}


# --- running a command's lead in Desktop -----------------------------------------------------------

def command_prompt(command: str, arguments: str, source: Path = pack.PACK_DIR) -> str:
    """The command's template from the pack, with $ARGUMENTS filled in: what Desktop sends."""
    text = (source / "commands" / f"{command}.md").read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
    return body.strip().replace("$ARGUMENTS", arguments)


def install_args(setup: str, components: list[str], gtu: list[str] | None = None) -> tuple[list[str], dict]:
    """The command that installs the pack for a setup, `gtu integrate --pack --only
    split,<components> [--lite]`, and the environment variables it needs."""
    chosen = SETUPS[setup]
    args = [*(gtu or ["gtu"]), "integrate", "--pack", "--only", ",".join(["split", *components])]
    if chosen["lite"]:
        args.append("--lite")
    return args, dict(chosen["env"])


def install(setup: str, components: list[str], gtu: list[str] | None = None, runner=subprocess.run) -> None:
    """Install the pack for a setup. Desktop picks it up when it next starts."""
    args, env = install_args(setup, components, gtu)
    runner(args, check=True, env=env_without_key(**env), stdin=subprocess.DEVNULL)


@dataclass
class RunResult:
    text: str
    seconds: float
    exit_code: int
    steps: int = 0
    tokens: dict = field(default_factory=dict)  # input/output/cache totals of the lead's own session
    error: str | None = None
    started: datetime | None = None  # when the run began, for tokens_by_model


def run_command(project: Path, lead: str, prompt: str, timeout: float = 3600.0,
                agent: desktop.DesktopAgent | None = None) -> RunResult:
    """The lead agent on the command's prompt, as a Desktop session in the project. Needs Desktop
    (run from its terminal); `agent` is for the tests."""
    began, started = datetime.now(timezone.utc), time.monotonic()
    try:
        agent = agent or desktop.DesktopAgent(desktop.discover(), timeout=timeout)
    except desktop.DesktopNotFound as exc:
        return RunResult("", 0.0, -1, error=f"{exc}. Run the benchmark from BharatCode Desktop's terminal.", started=began)
    result = agent.run_agent(project, lead, prompt)
    tokens = {"input": sum(s.input_tokens for s in result.steps),
              "output": sum(s.output_tokens + s.reasoning_tokens for s in result.steps),
              "cache_read": sum(s.cache_read_tokens for s in result.steps)}
    error = result.error or (f"stopped: {result.stopped}" if result.stopped else None)
    return RunResult(result.text, time.monotonic() - started, result.exit_code or 0, len(result.steps), tokens,
                     error, began)


def tokens_by_model(run: RunResult, project: Path) -> dict[str, int]:
    """Tokens per model in the project's Desktop sessions since the run began: the lead's and every
    helper's, exact, from Desktop's own record. Empty when Desktop's data isn't found."""
    figures = stats.tokens_since(run.started, Path(project)) if run.started else None
    return {model: used["tokens"] for model, used in (figures or {}).items() if used["tokens"] > 0}


# --- /deep-research ------------------------------------------------------------------------------

def latest_folder(project: Path, kind: str = "research") -> Path | None:
    root = Path(project) / kind
    folders = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    return max(folders, key=lambda p: p.stat().st_mtime) if folders else None


def research_tally(folder: Path, key_papers: list[str]) -> dict:
    """From a run's sources.json: how many references resolved (verify_refs verdicts), how many
    were dropped, and how many of the question's key papers (DOIs or arXiv ids) a kept source
    matches. Claims support needs a person: it is left null."""
    sources = []
    path = Path(folder) / "sources.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            sources = data.get("sources") if isinstance(data, dict) else data
        except ValueError:
            sources = []
    kept = [s for s in sources if s.get("verified") is True]
    checked = [s for s in sources if "verified" in s]
    keys = set()
    for s in kept:
        for value in (s.get("doi"), (s.get("resolved") or {}).get("doi")):
            if scholar.normalize_doi(value):
                keys.add("doi:" + scholar.normalize_doi(value))
        for value in (s.get("arxiv"), (s.get("resolved") or {}).get("arxiv"), s.get("url")):
            if scholar.normalize_arxiv(value):
                keys.add("arxiv:" + scholar.normalize_arxiv(value))
    wanted = {("doi:" + scholar.normalize_doi(k)) if scholar.normalize_doi(k) else ("arxiv:" + (scholar.normalize_arxiv(k) or k))
              for k in key_papers}
    covered = len(wanted & keys)
    return {
        "sources": len(sources), "checked": len(checked), "resolved": len(kept), "dropped": len(checked) - len(kept),
        "resolved_share": (len(kept) / len(checked)) if checked else None,
        "key_papers": len(wanted), "key_papers_covered": covered,
        "coverage": (covered / len(wanted)) if wanted else None,
        "claims_supported_share": None,  # a person reads a sample of claims against their sources
        "report_words": len((Path(folder) / "report.md").read_text(encoding="utf-8", errors="replace").split())
        if (Path(folder) / "report.md").is_file() else 0,
    }


# --- /code-review ----------------------------------------------------------------------------------

def findings_from_text(text: str) -> set[tuple[str, int]]:
    """Every file:line a review names (the lead's final reply)."""
    return {(path.replace("\\", "/"), int(line)) for path, line in FINDING.findall(text)}


def match_seeds(findings: set[tuple[str, int]], seeds: list[dict], tolerance: int = 3) -> tuple[list[dict], float]:
    """Which seeded bugs a review found: a finding matches a seed when it names the seed's file and
    a line within `tolerance` of it (the failing line can sit a few lines away from the mutation)."""
    hits = []
    for seed in seeds:
        file = seed["file"].replace("\\", "/")
        if any(path.endswith(file) and abs(line - int(seed["line"])) <= tolerance for path, line in findings):
            hits.append(seed)
    return hits, (len(hits) / len(seeds)) if seeds else 0.0


def precision(findings_judged: list[dict]) -> float | None:
    """Of the findings a person marked ("real": true/false), the share that are real; None until marked."""
    judged = [f for f in findings_judged if isinstance(f.get("real"), bool)]
    return (sum(1 for f in judged if f["real"]) / len(judged)) if judged else None


def apply_seed(repo: Path, seed: dict) -> None:
    """Apply a seed's unified diff to the repo's working tree (the bug under review)."""
    subprocess.run(["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-"], input=seed["patch"], text=True, check=True)


def reset_repo(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-fdq", "--exclude=.gtu", "--exclude=spare-gpu"], check=True)


# --- deciding switch-safety for a role --------------------------------------------------------------

def bootstrap_lower(diffs: list[float], seed: int = 7, rounds: int = BOOTSTRAP) -> float:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(rounds))
    return means[int(0.025 * rounds)]


def decide_role(pairs: list[tuple[float | None, float | None]]) -> dict:
    """pairs: (score with the heavy-model role, score with the -standard variant) per task. The
    rule of scripts/eval_models.py: switch-safe when the 95% paired-bootstrap lower bound of
    mean(standard - heavy) is at least -5% of the heavy mean; unmeasured when nothing scored."""
    usable = [(h, s) for h, s in pairs if h is not None and s is not None]
    if len(usable) < 2 or not any(h or s for h, s in usable):
        return {"verdict": "unmeasured", "tasks": len(usable)}
    heavy_mean = statistics.fmean(h for h, _ in usable)
    standard_mean = statistics.fmean(s for _, s in usable)
    diffs = [s - h for h, s in usable]
    lower = bootstrap_lower(diffs)
    delta = DELTA_SHARE * heavy_mean
    return {"verdict": "switch-safe" if lower >= -delta else "keep-preferred", "tasks": len(usable),
            "heavy_mean": heavy_mean, "standard_mean": standard_mean, "mean_diff": statistics.fmean(diffs),
            "ci95_lower": lower, "delta": delta}


def write_policy(path: Path, roles: dict[str, dict]) -> dict:
    """Record the roles' verdicts (and evidence) in routing_policy.json, leaving the job types alone."""
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    policy.setdefault("types", {})
    policy.setdefault("evidence", {})
    for role, result in roles.items():
        policy["types"][role] = result["verdict"]
        if result["verdict"] != "unmeasured":
            policy["evidence"][role] = {k: v for k, v in result.items() if k != "verdict"}
    Path(path).write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return policy


# --- results ------------------------------------------------------------------------------------

def markdown_table(rows: list[dict], columns: list[str]) -> str:
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in rows]
    return "\n".join(lines)
