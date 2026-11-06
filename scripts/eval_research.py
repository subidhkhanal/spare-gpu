"""The /deep-research benchmark (plan §8): every question in each setup, tallied from the files
the command leaves. Needs BharatCode's CLI signed in, the pack installed for the CLI (stable
channel), and a machine with internet access (the scouts search the web and the registries).

    python scripts/eval_research.py research/deep-research/tasks.json [--setups deepseek,split,lite,split-standard-roles]
        [--depth quick|standard|deep] [--project <folder>] [--write-policy]

tasks.json: [{"question": "...", "key_papers": ["10.1038/...", "1706.03762", ...]}, ...]
Writes research/deep-research/results-<date>.json and results.md next to the tasks file. Claims
support (90% of a sample must be backed by the cited source) needs a person: results.md has a
column to fill. --write-policy records research-checker's verdict (split vs
split-standard-roles coverage) in routing_policy.json. Costs real tokens; runs one task at a time.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gpu_token_utilization import evalpack  # noqa: E402

POLICY = Path(__file__).resolve().parents[1] / "src" / "gpu_token_utilization" / "routing_policy.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--setups", default="deepseek,split,lite,split-standard-roles")
    ap.add_argument("--depth", default="standard")
    ap.add_argument("--project", default=None, help="folder to run in (default: a fresh temp folder)")
    ap.add_argument("--write-policy", action="store_true")
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    out_dir = Path(args.tasks).parent
    project = Path(args.project) if args.project else Path(tempfile.mkdtemp(prefix="eval-research-"))
    results = []
    for setup in args.setups.split(","):
        evalpack.install(setup, ["research"])
        for i, task in enumerate(tasks, 1):
            before = evalpack.usage_snapshot()
            prompt = evalpack.command_prompt("deep-research", f"{task['question']} --depth {args.depth}")
            run = evalpack.run_command(project, "research-lead", prompt, setup)
            folder = evalpack.latest_folder(project, "research")
            tally = evalpack.research_tally(folder, task.get("key_papers", [])) if folder else {}
            row = {"setup": setup, "task": i, "question": task["question"], "minutes": round(run.seconds / 60, 1),
                   "exit": run.exit_code, "error": run.error, "tokens_by_model": evalpack.stats_delta(before, evalpack.usage_snapshot()),
                   "folder": str(folder) if folder else None, **tally}
            results.append(row)
            print(json.dumps(row), flush=True)
    stamp = date.today().isoformat()
    (out_dir / f"results-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    columns = ["setup", "task", "minutes", "resolved_share", "coverage", "dropped", "claims_supported_share", "tokens_by_model"]
    (out_dir / "results.md").write_text(f"# /deep-research results ({stamp})\n\n" + evalpack.markdown_table(results, columns)
                                       + "\n\nclaims_supported_share: fill in by hand from a sample of 30 claims per setup.\n", encoding="utf-8")
    if args.write_policy:
        by = {(r["setup"], r["task"]): r.get("coverage") for r in results}
        pairs = [(by.get(("split", t)), by.get(("split-standard-roles", t))) for t in range(1, len(tasks) + 1)]
        verdict = evalpack.decide_role(pairs)
        evalpack.write_policy(POLICY, {"research-checker": verdict})
        print("research-checker:", json.dumps(verdict))


if __name__ == "__main__":
    main()
