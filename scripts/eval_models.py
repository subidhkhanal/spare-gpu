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
The tests jobs run the task repo's test command; make sure it works there (for spare-gpu's own
repo: this venv first on PATH and PYTHONPATH=src), or every tests task scores 0 on both models.
A type where nothing scored on either model is "unmeasured", never switch-safe.

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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from gpu_token_utilization import desktop
from gpu_token_utilization.budget import Budgets
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
    folder = store.get_job(job_id).output_dir
    review = repo / folder / f"gtu-review-{job_id}.md" if folder else None
    report = review.read_text(encoding="utf-8") if review and review.is_file() else ""
    good = bad = 0
    for path, line in FINDING.findall(report):
        file = repo / path
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines() if file.is_file() else []
        good, bad = (good + 1, bad) if 0 < int(line) <= len(lines) else (good, bad + 1)
    return float(good - bad)


def run_task(conn, task: dict, model: str, workdir: Path, job_budget: int, log=print) -> tuple[float, int, bool]:
    """One task on one model in a fresh checkout; (score, tokens, paused by the budget)."""
    repo = checkout(task["repo"], task["sha"], workdir)
    with Store(repo / ".gtu" / "gtu.db") as store:
        job = store.add_job(task["kind"], task["target"], str(repo))
        store.set_route(job.id, model, model, "evaluation run")  # pinned: no routing during an eval
        config = JobConfig.from_env()
        agent = desktop.DesktopAgent(conn, model=f"bharatcode/{model}", timeout=config.agent_timeout)
        budgets = Budgets(per_job=job_budget)
        Scheduler(store, agent=agent, config=config, budgets=budgets, log=log).run_job(job.id)
        status = store.get_job(job.id).status
        if status == "failed":
            # A failed job (service unreachable, say) says nothing about the model: retry it, never score it 0.
            raise RuntimeError(f"{task['kind']} {task['target']} on {model}: job {job.id} failed")
        paused = status == "paused"
        return score(store, job.id, task["kind"], repo), store.request_summary(job.id)["total_tokens"], paused


def keep_awake() -> None:
    """Stop Windows idling into (Modern) Standby while the eval runs; released when the process exits."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def task_key(task: dict) -> str:
    return f"{task['kind']}|{task['target']}|{task['sha']}|{task['repo']}"


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
        if not any(r["scores"]["deepseek"] or r["scores"]["qwen"] for r in rows):
            # Nothing scored on either model (a broken test setup, say): no evidence, not "no difference".
            types[kind] = "unmeasured"
        else:
            types[kind] = "switch-safe" if lower >= -delta else "keep-preferred"
        evidence[kind] = {"tasks": len(rows),
                          "paused_runs": sum(bool(v) for r in rows for v in r.get("paused", {}).values()),
                          "deepseek_mean": statistics.mean(deepseek),
                          "qwen_mean": statistics.mean(r["scores"]["qwen"] for r in rows),
                          "mean_diff": statistics.mean(diffs), "ci95_lower": lower, "delta": delta}
    return {"types": types, "evidence": evidence}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "src" / "gpu_token_utilization"
                                             / "routing_policy.json"))
    parser.add_argument("--job-budget", type=int, default=1_000_000,
                        help="tokens per job before it pauses (default 1,000,000: an eval must let jobs finish, "
                             "or a budget cut scores 0 on both models and measures nothing)")
    parser.add_argument("--kinds", default="tests,docs,review", help="comma-separated job types to run")
    parser.add_argument("--parallel", type=int, default=4,
                        help="runs at once (default 4, about 2 per model; 1 = one at a time)")
    parser.add_argument("--fresh", action="store_true", help="ignore saved progress and run every task again")
    args = parser.parse_args()
    try:
        conn = desktop.discover()
    except desktop.DesktopNotFound as exc:
        sys.exit(f"{exc}. Run this from a terminal inside BharatCode Desktop.")
    keep_awake()
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    tasks = [t for t in json.loads(Path(args.tasks).read_text(encoding="utf-8")) if t["kind"] in kinds]
    # Progress is saved after every run, so a closed Desktop or a sleeping PC loses one run, not the eval.
    progress_file = Path(args.tasks).with_name("progress.json")
    progress = {} if args.fresh or not progress_file.is_file() else json.loads(progress_file.read_text(encoding="utf-8"))
    lock = threading.Lock()
    units = []
    for i, task in enumerate(tasks, 1):
        row = progress.setdefault(task_key(task), {**task, "scores": {}, "tokens": {}, "paused": {}})
        row.setdefault("paused", {})
        for label, model in MODELS.items():
            name = f"[{i}/{len(tasks)} {task['kind']} {Path(task['target']).name} {label}]"
            if label in row["scores"]:
                print(f"{name} score {row['scores'][label]} (saved earlier)")
            else:
                units.append((name, task, row, label, model))

    def run_unit(name, task, row, label, model):
        # Each run has its own checkout, job store and Desktop session, so runs are independent.
        # git worktrees can keep files locked briefly on Windows; a leftover temp folder is harmless
        with tempfile.TemporaryDirectory(prefix="gtu-eval-", ignore_cleanup_errors=True) as tmp:
            s, tokens, paused = run_task(conn, task, model, Path(tmp) / "repo", args.job_budget,
                                         log=lambda m: print(f"{name} {m}", flush=True))
        with lock:
            row["scores"][label], row["tokens"][label], row["paused"][label] = s, tokens, paused
            progress_file.write_text(json.dumps(progress, indent=2), encoding="utf-8")
            print(f"{name} score {s}, {tokens:,} tokens" + (" (paused by the budget)" if paused else ""), flush=True)

    print(f"{len(units)} run(s) to do, {args.parallel} at a time.", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = [pool.submit(run_unit, *u) for u in units]
        failed = 0
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # one broken run must not lose the others; it stays unsaved, so a re-run retries it
                failed += 1
                print(f"A run failed: {exc!r}", flush=True)
    if failed or any(len(progress[task_key(t)]["scores"]) < len(MODELS) for t in tasks):
        sys.exit(f"{failed} run(s) failed; progress is saved, run the same command again to retry them.")
    results = [progress[task_key(t)] for t in tasks]
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
