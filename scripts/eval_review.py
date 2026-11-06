"""The /code-review benchmark against seeded bugs (plan §8). Needs BharatCode's CLI signed in and
the pack installed for the CLI (stable channel).

    python scripts/eval_review.py research/code-review/seeds.json [--setups deepseek,split,lite,split-standard-roles]
        [--effort medium] [--write-policy]
    python scripts/eval_review.py --judge research/code-review/results-<date>.json   (after marking "real")

seeds.json: [{"repo": "<path to a checkout>", "file": "src/x.py", "line": 42, "what": "off by one",
              "patch": "<unified diff that plants the bug>"}, ...] plus clean diffs with "clean": true.
Each seed is applied to the checkout, reviewed, and reverted. Recall is computed here; precision
needs a person to mark each finding "real": true/false in the results JSON, then --judge.
The built-in /review baseline is run by hand (Desktop) and entered in results.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gpu_token_utilization import evalpack  # noqa: E402

POLICY = Path(__file__).resolve().parents[1] / "src" / "gpu_token_utilization" / "routing_policy.json"


def judge(path: Path) -> None:
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    by_setup: dict[str, list] = {}
    for row in results:
        by_setup.setdefault(row["setup"], []).extend(row.get("findings", []))
    for setup, findings in by_setup.items():
        print(f"{setup}: precision {evalpack.precision(findings)} over {len(findings)} finding(s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds", nargs="?")
    ap.add_argument("--setups", default="deepseek,split,lite,split-standard-roles")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--write-policy", action="store_true")
    ap.add_argument("--judge", default=None)
    args = ap.parse_args()
    if args.judge:
        judge(Path(args.judge))
        return
    seeds = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
    out_dir = Path(args.seeds).parent
    results = []
    for setup in args.setups.split(","):
        evalpack.install(setup, ["review"])
        for i, seed in enumerate(seeds, 1):
            repo = Path(seed["repo"])
            evalpack.reset_repo(repo)
            if not seed.get("clean"):
                evalpack.apply_seed(repo, seed)
            before = evalpack.usage_snapshot()
            run = evalpack.run_command(repo, "review-lead", evalpack.command_prompt("code-review", args.effort), setup)
            found = evalpack.findings_from_text(run.text)
            hits, recall = evalpack.match_seeds(found, [] if seed.get("clean") else [seed])
            row = {"setup": setup, "seed": i, "file": seed.get("file"), "clean": bool(seed.get("clean")), "minutes": round(run.seconds / 60, 1),
                   "exit": run.exit_code, "error": run.error, "found_seed": bool(hits), "findings_count": len(found),
                   "findings": [{"file": f, "line": n, "real": None} for f, n in sorted(found)],
                   "tokens_by_model": evalpack.stats_delta(before, evalpack.usage_snapshot()), "reply": run.text[-4000:]}
            results.append(row)
            evalpack.reset_repo(repo)
            print(json.dumps({k: v for k, v in row.items() if k not in ("reply", "findings")}), flush=True)
    stamp = date.today().isoformat()
    (out_dir / f"results-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = []
    for setup in args.setups.split(","):
        rows = [r for r in results if r["setup"] == setup]
        seeded = [r for r in rows if not r["clean"]]
        summary.append({"setup": setup, "recall": (sum(r["found_seed"] for r in seeded) / len(seeded)) if seeded else None,
                        "findings_on_clean_diffs": sum(r["findings_count"] for r in rows if r["clean"]),
                        "precision": None, "minutes_median": sorted(r["minutes"] for r in rows)[len(rows) // 2] if rows else None})
    (out_dir / "results.md").write_text(f"# /code-review results ({stamp})\n\n"
                                       + evalpack.markdown_table(summary, ["setup", "recall", "precision", "findings_on_clean_diffs", "minutes_median"])
                                       + "\n\nprecision: mark each finding's \"real\" in the results JSON, then `--judge`. "
                                       "Add the built-in /review row by hand.\n", encoding="utf-8")
    if args.write_policy:
        by = {(r["setup"], r["seed"]): (1.0 if r["found_seed"] else 0.0) for r in results if not r["clean"]}
        pairs = [(by.get(("split", s)), by.get(("split-standard-roles", s))) for s in range(1, len(seeds) + 1)]
        verdict = evalpack.decide_role(pairs)
        evalpack.write_policy(POLICY, {"review-verifier": verdict})
        print("review-verifier:", json.dumps(verdict))


if __name__ == "__main__":
    main()
