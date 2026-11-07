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
        bash = data["permission"].get("bash", "deny")  # no shell, or git's read-only commands only
        assert bash == "deny" or (bash["*"] == "deny" and all(p.startswith("git ") for p, a in bash.items() if a == "allow"))
        assert data.get("hidden") is True
        if data["mode"] == "subagent":  # only a lead may hand work to /auto's tools (--fix)
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
            helpers = set(pack.helper_names(component))
            if not helpers:
                assert "task" not in data["permission"], f"{lead} has no helpers to start"
                continue
            task = data["permission"]["task"]
            allowed = {name for name, action in task.items() if action == "allow"}
            assert task["*"] == "deny" and allowed == helpers, f"{lead} may start {helpers}"
            assert set(pack.helpers([component])) == helpers


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


def test_review_agents_read_only_and_the_lead_hands_fixes_to_a_job():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["review"]]) if k == "agent"}
    lead, lead_body = parse(files["review-lead"])
    assert "edit" not in lead["permission"] and lead["permission"]["spare-gpu_spare_delegate"] == "allow"
    assert lead["permission"]["bash"]["git diff*"] == "allow"
    flat = " ".join(lead_body.split())
    for step in ("low 2", "medium 4", "high 6", "deep 6 twice", "all finders in one message", "review-verifier",
                 "refute", 'type "fix"', "spare_delegate", "spare_waits", "Never claim a number you didn't measure"):
        assert step in flat, step
    finder, finder_body = parse(files["review-finder"])
    verifier, verifier_body = parse(files["review-verifier"])
    for data in (finder, verifier):
        assert "edit" not in data["permission"] and not any(k.startswith("spare-gpu_") for k in data["permission"])
    assert finder["model"] == FILLED.standard and verifier["model"] == FILLED.heavy
    assert "JSON list of candidates" in finder_body and "failure scenario" in finder_body
    assert "concrete input" in verifier_body and "critical" in verifier_body and "refute" in verifier_body


def test_security_agents_never_run_exploit_code_and_the_lead_filters_by_rule():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["security"]]) if k == "agent"}
    lead, lead_body = parse(files["security-lead"])
    assert "edit" not in lead["permission"] and lead["permission"]["spare-gpu_security_filter"] == "allow"
    assert "spare-gpu_spare_delegate" not in lead["permission"]  # a security review never applies changes
    flat = " ".join(lead_body.split())
    for step in ("five `security-finder` tasks in one message", "`security_filter` tool", "security-verifier", "refute",
                 "scored 8 or more", "exploit code", "read-only", "claude-code-security-review (MIT)", "Never claim a number"):
        assert step in flat, step
    finder, finder_body = parse(files["security-finder"])
    verifier, verifier_body = parse(files["security-verifier"])
    for data, body in ((finder, finder_body), (verifier, verifier_body)):
        assert "edit" not in data["permission"] and not any(k.startswith("spare-gpu_") for k in data["permission"])
        assert "exploit code" in body
    assert finder["model"] == FILLED.standard and verifier["model"] == FILLED.heavy
    assert "Environment variables and command-line flags are trusted" in verifier_body
    doc = pack.config(pack.select("security"), FILLED)
    assert doc["permission"]["spare-gpu_security_filter"] == "deny"
    assert set(doc["permission"]["task"]) == {"security-finder", "security-verifier", "security-verifier-standard"}


def test_simplify_accepts_only_behaviour_preserving_cleanups_and_applies_them_through_a_fix_job():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["simplify"]]) if k == "agent"}
    lead, lead_body = parse(files["simplify-lead"])
    assert "edit" not in lead["permission"] and lead["permission"]["spare-gpu_spare_delegate"] == "allow"
    flat = " ".join(lead_body.split())
    for step in ("behaviour", 'type "fix"', "kept the changes only if it passes" if False else "keeps the changes only if", "your_model",
                 "spare-gpu/<date>_<time>/", "Never edit files yourself", "Never claim a number"):
        assert step in flat, step
    finder, finder_body = parse(files["simplify-finder"])
    assert finder["model"] == FILLED.standard and "edit" not in finder["permission"]
    assert "behaviour_change" in finder_body and "JSON list" in finder_body
    assert pack.helpers([COMPONENTS["simplify"]]) == ["simplify-finder"]


def test_switchable_roles_get_a_standard_model_variant_made_from_the_same_file():
    files = pack.install_files(pack.select("review"), FILLED)
    names = [(k, n) for k, n, _ in files]
    assert ("agent", "review-verifier") in names and ("agent", "review-verifier-standard") in names
    heavy = dict(((k, n), t) for k, n, t in files)[("agent", "review-verifier")]
    variant = dict(((k, n), t) for k, n, t in files)[("agent", "review-verifier-standard")]
    assert f"model: {FILLED.heavy}" in heavy and f"model: {FILLED.standard}" in variant
    assert "(standard-model variant" in variant.split("\n---\n")[0] and "{{" not in variant
    assert variant.split("\n---\n", 1)[1] == heavy.split("\n---\n", 1)[1]  # the same body: it can't drift
    assert "review-verifier-standard" in pack.helpers(pack.select("review"))
    doc = pack.config(pack.select("review"), FILLED)
    assert doc["permission"]["task"]["review-verifier-standard"] == "deny" and doc["permission"]["spare-gpu_pack_cost"] == "deny"
    assert ("agent", "security-verifier-standard") in pack.all_markdown_names()
    assert pack.variant_of("review-finder") is None and pack.helper_names(COMPONENTS["verify"]) == ["verify-driver"]


def test_every_lead_picks_the_variant_from_spare_waits_and_reports_pack_cost():
    for component, lead_name in (("research", "research-lead"), ("review", "review-lead"), ("security", "security-lead")):
        files = {n: p for k, n, p in pack.markdown_files([COMPONENTS[component]]) if k == "agent"}
        lead, body = parse(files[lead_name])
        role = {"research": "research-checker", "review": "review-verifier", "security": "security-verifier"}[component]
        assert lead["permission"]["task"][role] == lead["permission"]["task"][role + "-standard"] == "allow"
        assert lead["permission"]["spare-gpu_pack_cost"] == "allow"
        flat = " ".join(body.split())
        assert f"`{role}-standard`" in flat and "`pack_cost` tool" in flat and "variant" in flat
    simplify, body = parse({n: p for k, n, p in pack.markdown_files([COMPONENTS["simplify"]]) if k == "agent"}["simplify-lead"])
    assert simplify["permission"]["spare-gpu_pack_cost"] == "allow" and "`pack_cost` tool" in " ".join(body.split())


def test_verify_app_drives_the_browser_through_playwright_and_judges_from_notes():
    files = {(k, n): p for k, n, p in pack.markdown_files([COMPONENTS["verify"]])}
    command, command_body = parse(files[("command", "verify-app")])
    assert command["agent"] == "verify-lead" and "If there is no URL" in command_body
    lead, lead_body = parse(files[("agent", "verify-lead")])
    assert lead["permission"]["task"] == {"*": "deny", "verify-driver": "allow"} and "bash" not in lead["permission"]
    assert all(p.startswith(("verify/", "*/verify/")) for p in edit_patterns(lead))
    flat = " ".join(lead_body.split())
    for step in ("you don't start servers", "all in one message", "Notes are data", "Unclear is a valid", "`pack_cost` tool", "Playwright"):
        assert step in flat, step
    driver, driver_body = parse(files[("agent", "verify-driver")])
    assert driver["model"] == FILLED.standard and driver["permission"]["playwright_*"] == "allow"
    assert "bash" not in driver["permission"] and all(p.endswith("steps-*.md") for p in edit_patterns(driver))
    assert "don't enter real credentials" in driver_body and "decide nothing" in driver_body
    assert pack.helpers([COMPONENTS["verify"]]) == ["verify-driver"] and not COMPONENTS["verify"].mcp  # Playwright comes from the marketplace


def test_every_leads_cost_summary_names_the_installed_models():
    files = dict(((k, n), t) for k, n, t in pack.install_files(pack.select(None), FILLED))
    leads = [n for k, n in files if k == "agent" and n.endswith("-lead")]
    assert len(leads) == 5
    for name in leads:
        flat = " ".join(files[("agent", name)].split())
        assert f"The standard model is {FILLED.standard} and the heavy model is {FILLED.heavy}" in flat, name


def test_the_lead_runs_the_plans_flow_and_reports_the_split():
    _, body = parse({n: p for k, n, p in pack.markdown_files([COMPONENTS["research"]]) if k == "agent"}["research-lead"])
    body = " ".join(body.split())
    for step in ("plan.md", "all in the same message", "outline.md", "sources.json", "one section", "verify_refs",
                 "DROP", "spare_waits", "queue time saved", "Never claim a number you didn't measure", "two rounds"):
        assert step in body, step


def test_the_pack_config_hides_the_research_helpers_and_tools_from_everyone_else():
    doc = pack.config(pack.select("research"), FILLED)
    assert doc["permission"]["task"] == {"research-scout": "deny", "research-writer": "deny", "research-checker": "deny",
                                         "research-checker-standard": "deny"}
    assert doc["permission"]["spare-gpu_scholar_search"] == doc["permission"]["spare-gpu_verify_refs"] == "deny"
    assert doc["mcp"] == {"exa": {"type": "remote", "url": "https://mcp.exa.ai/mcp", "oauth": False}}
