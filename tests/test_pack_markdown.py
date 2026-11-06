"""The pack's commands and agents as BharatCode will read them: frontmatter that parses, model
placeholders only, every agent denying everything first, leads as primary agents that may start
exactly their own helpers, helpers that may write only their own files, and the agents that read
the web or the notes told to treat them as data."""

import yaml
import pytest

from gpu_token_utilization import pack
from gpu_token_utilization.pack import COMPONENTS, HEAVY, MARKER, STANDARD, Models

FILLED = Models("bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b")
AUTO_TOOLS = ("spare-gpu_spare_delegate", "spare-gpu_spare_ask", "spare-gpu_spare_work_status", "spare-gpu_spare_cancel_work")


def present():
    return [(kind, name, path) for c in COMPONENTS.values() for kind, name, path in pack.markdown_files([c])]


def parse(path):
    text = FILLED.fill(path.read_text(encoding="utf-8"))
    assert text.startswith("---\n" + MARKER), path
    head, body = text.split("\n---\n", 1)
    return yaml.safe_load(head[4:]), body


def test_there_is_something_to_install():
    assert {(k, n) for k, n, _ in present()} >= {("command", "deep-research"), ("agent", "research-lead"), ("agent", "research-scout"),
                                                ("agent", "research-writer"), ("agent", "research-checker")}


@pytest.mark.parametrize("kind, name, path", present(), ids=lambda x: str(x) if isinstance(x, str) else "")
def test_every_file_parses_carries_the_marker_and_only_the_two_placeholders(kind, name, path):
    raw = path.read_text(encoding="utf-8")
    assert "{{" not in raw.replace(HEAVY, "").replace(STANDARD, "")
    data, body = parse(path)
    assert "{{" not in FILLED.fill(raw) and data["description"] and body.strip()
    if kind == "agent":
        assert data["mode"] in ("primary", "subagent") and data["model"] in (FILLED.heavy, FILLED.standard)
        assert data["permission"]["*"] == "deny"  # everything denied first, then what the agent needs
        assert "bash" not in data["permission"] or data["permission"]["bash"] == "deny"
        assert data.get("hidden") is True
        assert not any(data["permission"].get(t) == "allow" for t in AUTO_TOOLS)
    else:
        assert "$ARGUMENTS" in body and data.get("subtask") is not True


def test_a_commands_lead_is_a_primary_agent_that_may_start_exactly_its_helpers():
    for component in COMPONENTS.values():
        files = {(k, n): p for k, n, p in pack.markdown_files([component])}
        for command in component.commands:
            if ("command", command) not in files:
                continue
            lead = parse(files[("command", command)])[0]["agent"]
            assert ("agent", lead) in files, f"{command}: its agent {lead} is in the pack"
            data, _ = parse(files[("agent", lead)])
            assert data["mode"] == "primary"  # depth 0: the only place a task may start more tasks
            task = data["permission"]["task"]
            allowed = {name for name, action in task.items() if action == "allow"}
            helpers = set(component.agents) - {lead}
            assert task["*"] == "deny" and allowed == helpers, f"{lead} may start {helpers}"
            assert pack.helpers([component]) == sorted(helpers, key=component.agents.index)


def edit_patterns(data):
    rules = data["permission"]["edit"]
    assert rules["*"] == "deny"
    return [p for p, action in rules.items() if action == "allow"]


def test_research_agents_write_only_their_own_files_and_use_only_their_own_tools():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["research"]]) if k == "agent"}
    lead, _ = parse(files["research-lead"])
    assert all("research/" in p for p in edit_patterns(lead)) and lead["permission"]["spare-gpu_spare_waits"] == "allow"
    scout, scout_body = parse(files["research-scout"])
    assert all(p.endswith("notes/*") for p in edit_patterns(scout))
    assert scout["permission"]["spare-gpu_scholar_search"] == "allow" and scout["permission"]["exa_*"] == "allow"
    assert scout["permission"]["webfetch"] == "allow" and "spare-gpu_verify_refs" not in scout["permission"]
    writer, writer_body = parse(files["research-writer"])
    assert all(p.endswith("report.md") for p in edit_patterns(writer)) and "webfetch" not in writer["permission"]
    checker, checker_body = parse(files["research-checker"])
    assert all(p.endswith("review.md") for p in edit_patterns(checker))
    assert checker["permission"]["spare-gpu_verify_refs"] == "allow" and "spare-gpu_scholar_search" not in checker["permission"]
    assert scout["model"] == writer["model"] == FILLED.standard and lead["model"] == checker["model"] == FILLED.heavy
    for body in (scout_body, writer_body, checker_body):
        assert "are data" in body and ("an instruction" in body or "a command" in body)  # web text and notes are data


def test_the_lead_runs_the_plans_flow_and_reports_the_split():
    _, body = parse({n: p for k, n, p in pack.markdown_files([COMPONENTS["research"]]) if k == "agent"}["research-lead"])
    for step in ("plan.md", "all in the same message", "outline.md", "sources.json", "one section", "verify_refs",
                 "DROP", "spare_waits", "queue time saved", "Never claim a number you didn't measure", "two rounds"):
        assert step in body, step


def test_the_pack_config_hides_the_research_helpers_and_tools_from_everyone_else():
    doc = pack.config(pack.select("research"), FILLED)
    assert doc["permission"]["task"] == {"research-scout": "deny", "research-writer": "deny", "research-checker": "deny"}
    assert doc["permission"]["spare-gpu_scholar_search"] == doc["permission"]["spare-gpu_verify_refs"] == "deny"
    assert doc["mcp"] == {"exa": {"type": "remote", "url": "https://mcp.exa.ai/mcp"}}
