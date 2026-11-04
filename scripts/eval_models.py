"""Which job types may move from DeepSeek to Qwen? Measure, don't assume (writes routing_policy.json).

Runs every task in an evaluation set on BOTH models, through BharatCode Desktop and spare-gpu's own
job pipeline (same prompts, same permission rules, same checks), scores each result with the checks
the jobs already use, and applies a non-inferiority rule fixed in advance:

    a type is switch-safe if, over its tasks, the 95% paired-bootstrap interval of
    mean(Qwen score - DeepSeek score) lies above -delta, with delta = 5% of DeepSeek's mean score.

Scores (all objective):
    tests   passing tests kept (the verify step's count)
    docs    files whose documentation changes passed the docs check
    review  findings whose `path:line` exists in the reviewed code, minus ones that don't

Usage (from a terminal inside BharatCode Desktop, so its server and sign-in are reachable):
    python scripts/eval_models.py research/eval/tasks.json [--out src/gpu_token_utilization/routing_policy.json]

tasks.json: [{"repo": "<git url or path>", "sha": "<commit>", "kind": "tests|docs|review",
              "target": "<path in the repo>"}, ...]  (30-50 per type for a real decision)
Raw results go to research/eval/results-<date>.json next to the tasks file. Costs real tokens:
each task runs twice. Polite: tasks run one at a time.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from gpu_token_utilization import desktop, worktree
from gpu_token_utilization.gate import Gate, GateConfig
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler
from gpu_token_utilization.store import Store

MODELS = {"deepseek": "deepseek-v4.1-flash", "qwen": "qwen-3.8-27b"}
DELTA_SHARE = 0.05
BOOTSTRAP = 2000
FINDING = re.compile(r"`?([\w./\\-]+\.\w+):(\d+)`?")


def checkout(repo: str, sha: str, into: Path) -> Path:
    subprocess.run(["git", "clone", "--quiet", repo, str(into)], check=True)
    subprocess.run(["git", "-C", str(into), "checkout", "--quiet", sha], check=True)
    subprocess.run(["git", "-C", str(into), "checkout", "--quiet", "-b", "eval"], check=True)
    return into


def score(store: Store, job_id: str, kind: str, repo: Path) -> float:
    steps = {s["name"]: s["output"] or "" for s in store.steps(job_id)}
    if kind == "tests":
        m = re.search(r"Kept (\d+) passing", steps.get("verify", ""))
        return float(m.group(1)) if m else 0.0
    if kind == "docs":
        m = re.search(r"Kept documentation changes in (\d+) file", steps.get("verify", ""))
        return float(m.group(1)) if m else 0.0
    report = worktree._git(repo, "show", f"{worktree.branch_name(job_id)}:gtu-review-{job_id}.md").stdout \
        if steps.get("review") else ""
    good = bad = 0
    for path, line in FINDING.findall(report):
        file = repo / path
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines() if file.is_file() else []
        good, bad = (good + 1, bad) if 0 < int(line) <= len(lines) else (good, bad + 1)
    return float(good - bad)


def run_task(conn, task: dict, model: str, workdir: Path) -> tuple[float, int]:
    """One task on one model in a fresh checkout; (score, tokens)."""
    repo = checkout(task["repo"], task["sha"], workdir)
    with Store(repo / ".gtu" / "gtu.db") as store:
        job = store.add_job(task["kind"], task["target"], str(repo))
        store.set_route(job.id, model, model, "evaluation run")  # pinned: no routing during an eval
        agent = desktop.DesktopAgent(conn, model=f"bharatcode/{model}")
        Scheduler(store, Gate(store, GateConfig()), None, cli=agent, config=JobConfig.from_env(),
                  ignore_gate=True, log=lambda m: print("   ", m)).run()
        return score(store, job.id, task["kind"], repo), store.request_summary(job.id)["total_tokens"]


def bootstrap_lower(diffs: list[float], seed: int = 7) -> float:
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(BOOTSTRAP))
    return means[int(0.025 * BOOTSTRAP)]


def decide(results: list[dict]) -> dict:
    types, evidence = {}, {}
    for kind in ("tests", "docs", "review"):
        rows = [r for r in results if r["kind"] == kind]
        if len(rows) < 2:
            types[kind] = "unmeasured"
            continue
        deepseek = [r["scores"]["deepseek"] for r in rows]
        diffs = [r["scores"]["qwen"] - r["scores"]["deepseek"] for r in rows]
        delta = DELTA_SHARE * abs(statistics.mean(deepseek))
        lower = bootstrap_lower(diffs)
        types[kind] = "switch-safe" if lower >= -delta else "keep-preferred"
        evidence[kind] = {"tasks": len(rows), "deepseek_mean": statistics.mean(deepseek),
                          "qwen_mean": statistics.mean(r["scores"]["qwen"] for r in rows),
                          "mean_diff": statistics.mean(diffs), "ci95_lower": lower, "delta": delta}
    return {"types": types, "evidence": evidence}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "src" / "gpu_token_utilization"
                                             / "routing_policy.json"))
    args = parser.parse_args()
    try:
        conn = desktop.discover()
    except desktop.DesktopNotFound as exc:
        sys.exit(f"{exc}. Run this from a terminal inside BharatCode Desktop.")
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    results = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task['kind']} {task['target']} @ {task['sha'][:10]}")
        row = {**task, "scores": {}, "tokens": {}}
        for label, model in MODELS.items():
            # git worktrees can keep files locked briefly on Windows; a leftover temp folder is harmless
            with tempfile.TemporaryDirectory(prefix="gtu-eval-", ignore_cleanup_errors=True) as tmp:
                row["scores"][label], row["tokens"][label] = run_task(conn, task, model, Path(tmp) / "repo")
            print(f"   {label}: score {row['scores'][label]}, {row['tokens'][label]:,} tokens")
        results.append(row)
    today = datetime.now(timezone.utc).date().isoformat()
    raw = Path(args.tasks).with_name(f"results-{today}.json")
    raw.write_text(json.dumps(results, indent=2), encoding="utf-8")
    policy = {"version": 1, "date": today,
              "note": f"Measured by scripts/eval_models.py on {len(tasks)} task(s); raw results in {raw.name}. "
                      "Switch-safe: 95% paired-bootstrap lower bound of mean(Qwen - DeepSeek) >= -5% of "
                      "DeepSeek's mean. Re-run when BharatCode changes its models.",
              **decide(results)}
    Path(args.out).write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(policy["types"], indent=2))


if __name__ == "__main__":
    main()
