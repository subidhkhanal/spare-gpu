"""The /code-review benchmark against seeded bugs (plan §8). Run it from BharatCode Desktop's
terminal, once per setup (deepseek, split, lite, split-standard-roles): install the pack for the
setup first (the script prints the command), restart Desktop, then run.

    python scripts/eval_review.py research/code-review/seeds.json --setup split
        [--effort medium] [--write-policy]
    python scripts/eval_review.py --judge research/code-review/results-<date>.json   (after marking "real")

seeds.json: [{"repo": "<path to a checkout>", "file": "src/x.py", "line": 42, "what": "off by one",
              "patch": "<unified diff that plants the bug>"}, ...] plus clean diffs with "clean": true.
Each seed is applied to the checkout, reviewed, and reverted. Recall is computed here; precision
needs a person to mark each finding "real": true/false in the results JSON, then --judge. Each
run adds the setup's rows to results-<date>.json (a rerun of a setup replaces them).
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
    ap.add_argument("--setup", help="the setup the pack is installed as now")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--write-policy", action="store_true")
    ap.add_argument("--judge", default=None)
    args = ap.parse_args()
    if args.judge:
        judge(Path(args.judge))
        return
    if not args.seeds or not args.setup:
        ap.error("give the seeds file and --setup (or --judge <results file>)")
    seeds = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
    out_dir = Path(args.seeds).parent
    setup = args.setup
    check_setup(setup, "review")
    stamp = date.today().isoformat()
    results_file = out_dir / f"results-{stamp}.json"
    results = load_rows(results_file, setup)
    for i, seed in enumerate(seeds, 1):
        repo = Path(seed["repo"])
        evalpack.reset_repo(repo)
        if not seed.get("clean"):
            evalpack.apply_seed(repo, seed)
        run = evalpack.run_command(repo, "review-lead", evalpack.command_prompt("code-review", args.effort))
        found = evalpack.findings_from_text(run.text)
        hits, recall = evalpack.match_seeds(found, [] if seed.get("clean") else [seed])
        row = {"setup": setup, "seed": i, "file": seed.get("file"), "clean": bool(seed.get("clean")), "minutes": round(run.seconds / 60, 1),
               "exit": run.exit_code, "error": run.error, "found_seed": bool(hits), "findings_count": len(found),
               "findings": [{"file": f, "line": n, "real": None} for f, n in sorted(found)],
               "tokens_by_model": evalpack.tokens_by_model(run, repo), "reply": run.text[-4000:]}
        results.append(row)
        evalpack.reset_repo(repo)
        print(json.dumps({k: v for k, v in row.items() if k not in ("reply", "findings")}), flush=True)
    results_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = []
    for setup in dict.fromkeys(r["setup"] for r in results):
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
