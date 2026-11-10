"""The improvement loop, one step per command (see src/gpu_token_utilization/improve.py).

    python scripts/improve.py list
    python scripts/improve.py <target> evaluate [--candidate ID] [--only ID,ID]
    python scripts/improve.py <target> propose
    python scripts/improve.py <target> decide ID
    python scripts/improve.py <target> install [--candidate ID]        (prompts targets)
    python scripts/improve.py <target> promote ID [--force]
    python scripts/improve.py <target> rollback
    python scripts/improve.py <target> bar                              (replay targets)
    python scripts/improve.py <target> history

A settings target (verify-refs, router), start to finish:
    evaluate, propose (it searches and saves a candidate only if one wins), evaluate --candidate ID,
    decide ID, promote ID, then run the tests and commit when you are happy.
A prompts target (code-review, security-review, simplify, auto-handoff, deep-research) needs
BharatCode Desktop; run it from Desktop's terminal:
    install, restart Desktop, evaluate (the current prompts' score and failures), propose (the
    heavy model rewrites the prompts from those failures), install --candidate ID, restart
    Desktop, evaluate --candidate ID, decide ID, then promote ID or install (to put the current
    prompts back), and restart Desktop again.
Nothing is committed or pushed. A promotion is archived first; rollback puts it back.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gpu_token_utilization import improve  # noqa: E402


def show_score(score, path=None) -> None:
    print(f"score {score.value:.4f}" + (f"  (recorded in {path})" if path else ""))
    for key, value in score.detail.items():
        print(f"  {key}: {value}")
    short = [t for t in score.tasks if t.get("score", 1) < 1 and "decisions" not in t]  # the router's replay has no maximum
    if short:
        print(f"  {len(short)} of {len(score.tasks)} task(s) fell short:")
        for task in short[:25]:
            extra = {k: v for k, v in task.items() if k not in ("id", "score", "tokens_by_model") and v not in (None, [], "")}
            print(f"    {task['id']}: {task['score']}  {json.dumps(extra, ensure_ascii=False)[:300]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a target name, or 'list'")
    ap.add_argument("step", nargs="?", default="evaluate",
                    choices=["evaluate", "propose", "decide", "install", "promote", "rollback", "bar", "history"])
    ap.add_argument("id", nargs="?", help="a candidate id (decide, promote)")
    ap.add_argument("--candidate", help="evaluate or install this candidate instead of the current version")
    ap.add_argument("--only", help="comma-separated task ids (live targets): a quick partial run")
    ap.add_argument("--force", action="store_true", help="promote even without a 'better' decision")
    args = ap.parse_args()
    if args.target == "list":
        for t in improve.TARGETS.values():
            print(f"{t.name:16} {t.kind:8} {'live (Desktop)' if t.live else 'replay':15} {t.about}")
        return
    try:
        t = improve.target(args.target)
        if args.step == "evaluate":
            live = {"only": args.only.split(",")} if args.only and t.live else {}
            score, path = improve.evaluate(t.name, args.candidate, **live)
            show_score(score, path)
        elif args.step == "propose":
            if t.kind == "params":
                cid, result = improve.search_params(t.name)
                print(json.dumps(result, indent=2))
                print(f"candidate {cid}: evaluate it, decide, then promote" if cid else
                      "No setting in the grid beats the current ones by the margin: nothing saved.")
            elif t.kind == "prompts":
                cid = improve.propose_prompts(t.name)
                print(f"candidate {cid} saved in {improve.folder(t) / 'candidates' / cid}. Read its files, then: "
                      f"install --candidate {cid}, restart Desktop, evaluate --candidate {cid}, decide {cid}.")
            else:
                print(f"{t.name} is code: change the code, run `evaluate`, and keep the change only if it doesn't "
                      "fall below the bar (`bar` records a new one).")
        elif args.step == "decide":
            print(json.dumps(improve.decide(t.name, args.id), indent=2))
        elif args.step == "install":
            for line in improve.install(t.name, args.candidate):
                print(line)
            print("Fully restart BharatCode Desktop before evaluating.")
        elif args.step == "promote":
            entry = improve.promote(t.name, args.id, force=args.force)
            print(json.dumps(entry, indent=2))
            print("Promoted. Run the tests" + (", install and restart Desktop" if t.live else "") +
                  "; commit when you're happy. `rollback` undoes it.")
        elif args.step == "rollback":
            print(json.dumps(improve.rollback(t.name), indent=2))
        elif args.step == "bar":
            print(json.dumps(improve.record_bar(t.name), indent=2))
        elif args.step == "history":
            for entry in improve.history(name=t.name):
                print(json.dumps(entry))
    except (improve.ImproveError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
