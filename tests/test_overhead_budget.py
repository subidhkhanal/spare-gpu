"""The pack's per-step overhead budget (at most 1,500 tokens added to a normal session).

Two proofs: the static one, from the pack's own config (every tool and helper it adds is denied
for every agent but its own, so nothing is added to a normal request); and the measured one,
from research/pack-install/overhead.json once scripts/check_overhead_budget.py has been run on a
Windows machine with BharatCode's CLI. CI fails when the recorded measurement is over budget."""

import json
from pathlib import Path

import pytest

from gpu_token_utilization import pack

BUDGET = 1_500
RECORDED = Path(__file__).resolve().parents[1] / "research" / "pack-install" / "overhead.json"
MODELS = pack.Models("bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b")


def test_the_installed_pack_adds_nothing_to_a_normal_request_by_construction():
    doc = pack.config(pack.select(None), MODELS)
    assert pack.pack_added_tokens(doc) == 0
    tools = {t for c in pack.COMPONENTS.values() for t in c.tools}
    assert len(tools) == 4 and all(doc["permission"][f"spare-gpu_{t}"] == "deny" for t in tools)
    assert set(doc["permission"]["task"]) == set(pack.helpers(pack.COMPONENTS.values()))
    assert doc["mcp"] and all(doc["permission"][f"{server}_*"] == "deny" for server in doc["mcp"])  # e.g. Exa's search


def test_a_config_that_stopped_denying_would_show_the_cost():
    doc = pack.config(pack.select(None), MODELS)
    doc["permission"].pop("spare-gpu_scholar_search")
    doc["permission"]["task"].pop("research-scout")
    assert pack.pack_added_tokens(doc) == pack.TOKENS_PER_MCP_TOOL + pack.TOKENS_PER_SUBAGENT_LINE
    assert pack.pack_added_tokens({}) > BUDGET  # with no pack config at all, the pack would blow the budget
    shared = pack.config(pack.select(None), MODELS)
    shared["permission"].pop("spare-gpu_pack_cost")  # listed by every lead's component, described once
    assert pack.pack_added_tokens(shared) == pack.TOKENS_PER_MCP_TOOL
    exa = pack.config(pack.select(None), MODELS)
    exa["permission"].pop("exa_*")  # the pack's own MCP server, left visible: each of its tools costs every step
    assert pack.pack_added_tokens(exa) == pack.MCP_TOOL_COUNT["exa"] * pack.TOKENS_PER_MCP_TOOL


@pytest.mark.skipif(not RECORDED.exists(), reason="no measurement recorded yet: run scripts/check_overhead_budget.py on Windows")
def test_the_recorded_measurement_is_within_budget():
    result = json.loads(RECORDED.read_text(encoding="utf-8"))
    assert result["variant"] == "empty-mcp" and result["added"] == result["with_pack"] - result["without_pack"]
    assert result["added"] <= BUDGET, f"the pack adds {result['added']} tokens per step; the budget is {BUDGET}"
