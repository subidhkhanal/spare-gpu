"""The README's routing claims match the shipped policy: a job type moves to Qwen only when it was
measured switch-safe, and questions, which the policy doesn't measure, are said to go to the
faster model by design rather than by measurement."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
POLICY = json.loads((ROOT / "src" / "gpu_token_utilization" / "routing_policy.json").read_text(encoding="utf-8"))


def test_questions_are_routed_by_design_and_the_readme_says_so():
    assert not any(kind.startswith("question") for kind in POLICY["types"])  # the policy measures job types only
    assert "Questions aren't measured" in README
    assert re.search(r"questions, which go to the faster model by design rather than by measurement", README)
    assert not re.search(r"questions[^.\n]*were measured", README, re.I)


def test_the_readme_names_exactly_the_measured_switch_safe_job_types():
    jobs = {kind: verdict for kind, verdict in POLICY["types"].items() if kind in ("tests", "docs", "review")}
    safe = {kind for kind, verdict in jobs.items() if verdict == "switch-safe"}
    assert safe == {"docs"}, "the README's wording describes docs as the one measured type; update both together"
    assert "docs jobs, the one job type where Qwen was measured to be just as good" in README
    roles = set(POLICY["types"]) - set(jobs)  # the pack's switchable roles: unmeasured until the benchmarks run
    assert roles == {"research-checker", "review-verifier", "security-verifier"}
    assert all(POLICY["types"][r] == "unmeasured" for r in roles) and "standard-model variant" in README
    for kept in set(jobs) - safe:
        assert kept.rstrip("s") in "tests and reviews stay on DeepSeek", kept
