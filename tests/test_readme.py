"""The README's routing claims match the shipped policy: /auto's conversations, which the policy
doesn't measure, go to the faster model by design, and fix jobs, which aren't switch-safe, stay on
DeepSeek."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
POLICY = json.loads((ROOT / "src" / "gpu_token_utilization" / "routing_policy.json").read_text(encoding="utf-8"))
ROLES = {"research-checker", "review-verifier", "security-verifier"}  # the pack's switchable roles


def routing_section() -> str:
    return README.split("### How it picks the model", 1)[1].split("\n## ", 1)[0]


def test_questions_are_routed_by_design_and_the_readme_says_so():
    assert not any(kind.startswith("question") for kind in POLICY["types"])  # the policy doesn't cover /auto
    assert re.search(r"Moving down to Qwen happens only for `/auto` conversations, which go to the faster model "
                     r"by design rather than by measurement", README)
    assert not re.search(r"questions[^.\n]*were measured", README, re.I)


def test_fix_jobs_stay_on_deepseek_in_the_policy_and_the_readme():
    assert set(POLICY["types"]) == {"fix"} | ROLES
    assert POLICY["types"]["fix"] == "keep-preferred"
    assert all(POLICY["types"][r] == "unmeasured" for r in ROLES) and "standard-model variant" in README
    section = routing_section()
    assert "Fix jobs stay on DeepSeek" in section
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line]
    assert rows[0] == "| Right now | `/auto` runs on | Fix jobs run on |"
    assert all(row.rstrip(" |").endswith("DeepSeek") for row in rows[1:]) and len(rows) == 4


def test_the_readme_names_no_job_type_that_is_gone():
    for gone in ("docs job", "tests job", "review job", "measured to be just as good", "make the tests pass",
                 "green_rounds", "GTU_GREEN_ROUNDS", "/auto write tests", "implement docs/plan.md", "research/eval/"):
        assert gone not in README, gone
