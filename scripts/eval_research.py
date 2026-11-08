"""The /deep-research benchmark (plan §8): every question in one setup, tallied from the files the
command leaves. Run it from BharatCode Desktop's terminal, once per setup (deepseek, split, lite,
split-standard-roles): install the pack for the setup first (the script prints the command),
restart Desktop, then run. Needs internet access (the scouts search the web and the registries).

    python scripts/eval_research.py research/deep-research/tasks.json --setup split
        [--depth quick|standard|deep] [--project <folder>] [--write-policy]

tasks.json: [{"question": "...", "key_papers": ["10.1038/...", "1706.03762", ...]}, ...]
Adds the setup's rows to research/deep-research/results-<date>.json (a rerun of a setup replaces
its rows) and rewrites results.md next to the tasks file. Claims
support (90% of a sample must be backed by the cited source) needs a person: results.md has a
column to fill. --write-policy records research-checker's verdict (split vs
split-standard-roles coverage, once both are in the file) in routing_policy.json. Costs real
tokens; runs one task at a time.
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


def load_rows(path: Path, setup: str) -> list[dict]:
    """Today's results so far, minus any earlier rows of this setup (a rerun replaces them)."""
    rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    return [r for r in rows if r.get("setup") != setup]


def check_setup(setup: str, component: str) -> None:
    if setup not in evalpack.SETUPS:
        raise SystemExit(f"unknown setup {setup!r}; use one of {', '.join(evalpack.SETUPS)}")
    args, env = evalpack.install_args(setup, [component])
    where = " ".join(f"$env:{k}='{v}';" for k, v in env.items())
    print(f"Setup {setup}: the pack must be installed as `{(where + ' ') if where else ''}{' '.join(args)}` "
          "and BharatCode Desktop restarted since.", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--setup", required=True, help="the setup the pack is installed as now")
    ap.add_argument("--depth", default="standard")
    ap.add_argument("--project", default=None, help="folder to run in (default: a fresh temp folder)")
    ap.add_argument("--write-policy", action="store_true")
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    out_dir = Path(args.tasks).parent
    project = Path(args.project) if args.project else Path(tempfile.mkdtemp(prefix="eval-research-"))
    setup = args.setup
    check_setup(setup, "research")
    stamp = date.today().isoformat()
    results_file = out_dir / f"results-{stamp}.json"
    results = load_rows(results_file, setup)
    for i, task in enumerate(tasks, 1):
        prompt = evalpack.command_prompt("deep-research", f"{task['question']} --depth {args.depth}")
        run = evalpack.run_command(project, "research-lead", prompt)
        folder = evalpack.latest_folder(project, "research")
        tally = evalpack.research_tally(folder, task.get("key_papers", [])) if folder else {}
        row = {"setup": setup, "task": i, "question": task["question"], "minutes": round(run.seconds / 60, 1),
               "exit": run.exit_code, "error": run.error, "tokens_by_model": evalpack.tokens_by_model(run, project),
               "folder": str(folder) if folder else None, **tally}
        results.append(row)
        print(json.dumps(row), flush=True)
    results_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
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
