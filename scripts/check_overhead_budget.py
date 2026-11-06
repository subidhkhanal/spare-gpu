"""Does the command pack stay within its per-step overhead budget? (plan: at most 1,500 tokens
added to a normal session).

Measure twice with scripts/measure_overhead.py on the same machine, the `empty-mcp` variant (the
harness with spare-gpu's MCP server registered): once with the pack uninstalled, once after
`gtu integrate --pack` and a Desktop restart. Then:

    python scripts/check_overhead_budget.py before.jsonl after.jsonl

It writes research/pack-install/overhead.json with both numbers and the difference; the test
tests/test_overhead_budget.py fails CI when that file shows more than the budget.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

BUDGET = 1_500
OUT = Path(__file__).resolve().parents[1] / "research" / "pack-install" / "overhead.json"


def context_tokens(path: str) -> int:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            run = json.loads(line)
        except ValueError:
            continue
        if run.get("variant") == "empty-mcp" and run.get("steps"):
            first = run["steps"][0]
            return first["input"] + first.get("cache_read", 0) + first.get("cache_write", 0)
    raise SystemExit(f"{path}: no empty-mcp run with steps found")


def main(before: str, after: str) -> int:
    a, b = context_tokens(before), context_tokens(after)
    result = {"date": date.today().isoformat(), "variant": "empty-mcp", "without_pack": a, "with_pack": b,
              "added": b - a, "budget": BUDGET, "within_budget": b - a <= BUDGET}
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["within_budget"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
