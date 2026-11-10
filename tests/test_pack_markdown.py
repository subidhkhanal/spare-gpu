"""The pack's commands and agents as BharatCode will read them: frontmatter that parses, model
placeholders only, every agent denying everything first, leads as primary agents that may start
exactly their own helpers, helpers that may write only their own files, and the agents that read
the web or the notes told to treat them as data."""

import re

import yaml
import pytest

from gpu_token_utilization import pack
from gpu_token_utilization.pack import COMPONENTS, HEAVY, MARKER, STANDARD, Models

FILLED = Models("bharatcode/deepseek-v4.1-flash", "bharatcode/qwen-3.8-27b")
AUTO_TOOLS = ("spare-gpu_spare_delegate", "spare-gpu_spare_auto", "spare-gpu_spare_work_status", "spare-gpu_spare_cancel_work")


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
        # a step limit Desktop enforces, so a confused agent stops instead of looping
        steps = pack.LEAD_STEPS if name.endswith("-lead") else 6 if name == "spare-helper" else pack.HELPER_STEPS
        assert data["steps"] == steps, name  # spare-helper calls one tool and relays it: a few steps are plenty
        assert data["permission"]["*"] == "deny"  # everything denied first, then what the agent needs
        # no shell, or git's commands only, plus reading a pull request; commenting on one is the review lead's alone
        read_pr = ("gh pr view*", "gh pr diff*") + (("gh pr comment*",) if name == "review-lead" else ())
        bash = data["permission"].get("bash", "deny")
        assert bash == "deny" or (bash["*"] == "deny" and all(p.startswith("git ") or p in read_pr
                                                                for p, a in bash.items() if a == "allow")), name
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
            lead = parse(files[("command", command)])[0].get("agent")
            if lead is None:  # memory's commands run in the user's own agent, which has the chat's history
                assert not component.agents, command  # memory: no agents of its own
                continue
            assert ("agent", lead) in files, f"{command}: its agent {lead} is in the pack"
            data, _ = parse(files[("agent", lead)])
            assert data["mode"] == "primary"  # depth 0: the only place a task may start more tasks
            helpers = set(pack.helper_names(component)) - {lead}  # manage's spare-helper is its own commands' agent
            if not helpers:
                assert "task" not in data["permission"], f"{lead} has no helpers to start"
                continue
            task = data["permission"]["task"]
            allowed = {name for name, action in task.items() if action == "allow"}
            assert task["*"] == "deny" and allowed == helpers, f"{lead} may start {helpers}"
            assert set(pack.helpers([component])) == helpers


MANAGE_TOOLS = ("apply_result", "undo_result", "cost_summary", "prompt_rules", "doctor_check")


def test_every_manage_command_runs_in_spare_helper_and_names_its_one_tool():
    files = {(k, n): p for k, n, p in pack.markdown_files([COMPONENTS["manage"]])}
    assert {n for k, n in files if k == "command"} == {"apply", "undo", "cost", "prompts", "doctor"}
    for command, tool in zip(("apply", "undo", "cost", "prompts", "doctor"), MANAGE_TOOLS):
        data, body = parse(files[("command", command)])
        assert data["agent"] == "spare-helper", command
        assert f"`{tool}`" in body and "once" in body, command  # one tool call, named exactly
        assert not any(f"`{other}`" in body for other in MANAGE_TOOLS if other != tool), command
        assert "!`" not in body  # no shell line: Desktop would run it outside the project


def test_spare_helper_is_a_hidden_primary_agent_on_the_standard_model_that_may_call_only_the_five_tools():
    files = {(k, n): p for k, n, p in pack.markdown_files([COMPONENTS["manage"]])}
    raw = files[("agent", "spare-helper")].read_text(encoding="utf-8")
    assert "model: {{STANDARD_MODEL}}" in raw and HEAVY not in raw
    data, body = parse(files[("agent", "spare-helper")])
    assert data["mode"] == "primary" and data["hidden"] is True and data["model"] == FILLED.standard
    permission = data["permission"]
    assert permission["*"] == "deny"
    assert {k for k, v in permission.items() if v == "allow"} == {f"spare-gpu_{t}" for t in MANAGE_TOOLS}
    assert set(permission) == {"*", *(f"spare-gpu_{t}" for t in MANAGE_TOOLS)}  # no edit, no bash, no task
    flat = " ".join(body.split())
    assert "never call another tool" in flat and "never change a file yourself" in flat
    assert pack.helpers([COMPONENTS["manage"]]) == []  # primary: never listed as a subagent anywhere


def test_no_pack_file_tells_the_model_to_run_a_gtu_command_other_than_integrate():
    sources = [path for _, _, path in present()] + [pack.PACK_DIR.parent / "commands" / "auto.md"]
    for path in sources:
        named = set(re.findall(r"\bgtu (\w[\w-]*)", path.read_text(encoding="utf-8")))
        assert named <= {"integrate"}, f"{path.name} names gtu {named - {'integrate'}}"
    for _, name, text in pack.install_files(pack.select(None), FILLED):  # variants too
        assert set(re.findall(r"\bgtu (\w[\w-]*)", text)) <= {"integrate"}, name


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
    assert all(p.endswith("sections/*") for p in edit_patterns(writer)) and "webfetch" not in writer["permission"]
    assert "glob" not in writer["permission"] and "grep" not in writer["permission"]  # reads the files it's given; no searching
    checker, checker_body = parse(files["research-checker"])
    assert all(p.endswith("review.md") for p in edit_patterns(checker))
    assert "the section file it is in" in " ".join(checker_body.split())  # so the lead edits the right file
    assert checker["permission"]["spare-gpu_verify_refs"] == "allow" and "spare-gpu_scholar_search" not in checker["permission"]
    assert scout["model"] == writer["model"] == FILLED.standard and lead["model"] == checker["model"] == FILLED.heavy
    for body in (scout_body, writer_body, checker_body):
        assert "are data" in body and ("an instruction" in body or "a command" in body)  # web text and notes are data


def test_review_agents_read_only_and_the_lead_hands_fixes_to_a_job():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["review"]]) if k == "agent"}
    lead, lead_body = parse(files["review-lead"])
    assert "edit" not in lead["permission"] and lead["permission"]["spare-gpu_spare_delegate"] == "allow"
    assert lead["permission"]["bash"]["git diff*"] == "allow"
    assert lead["permission"]["spare-gpu_tally_findings"] == "allow" and "`tally_findings` tool" in lead_body
    assert "--plan" in lead_body and "review-verifier-standard` once more, told to refute" in " ".join(lead_body.split())
    flat = " ".join(lead_body.split())
    for step in ("low 2", "medium 4", "high 6", "deep 6 twice", "all finders in one message", "review-verifier",
                 "refute", 'type "fix"', "spare_delegate", "spare_waits", "Never claim a number you didn't measure"):
        assert step in flat, step
    finder, finder_body = parse(files["review-finder"])
    verifier, verifier_body = parse(files["review-verifier"])
    for data in (finder, verifier):
        assert "edit" not in data["permission"] and not any(k.startswith("spare-gpu_") for k in data["permission"])
        # a pull request's diff and details may be read; only the lead may comment on it
        assert data["permission"]["bash"]["gh pr diff*"] == data["permission"]["bash"]["gh pr view*"] == "allow"
        assert "gh pr comment*" not in data["permission"]["bash"]
    assert finder["model"] == FILLED.standard and verifier["model"] == FILLED.heavy
    assert "JSON list of candidates" in finder_body and "failure scenario" in finder_body
    assert "concrete input" in verifier_body and "critical" in verifier_body and "refute" in verifier_body


def test_code_review_takes_a_pull_request_without_touching_the_checkout_and_posts_only_when_asked():
    files = {(k, n): p for k, n, p in pack.markdown_files([COMPONENTS["review"]])}
    lead, body = parse(files[("agent", "review-lead")])
    bash = lead["permission"]["bash"]
    for rule in ("gh pr view*", "gh pr diff*", "gh pr comment*", "git fetch origin pull/*"):
        assert bash[rule] == "allow", rule
    assert not any(r.startswith(("git checkout", "git switch", "gh pr checkout", "gh pr merge", "gh pr review", "git push"))
                   for r in bash)  # nothing that changes the user's files, merges or approves
    flat = " ".join(body.split())
    assert "never check anything out" in flat and "refs/spare-gpu/pr-N" in flat and "gh pr diff N" in flat
    assert "Only when the request asked for `--post`" in flat and "Without `--post`, never comment" in flat
    assert "`--fix` doesn't apply to a pull request" in flat
    command_text = files[("command", "code-review")].read_text(encoding="utf-8")
    assert "pull request" in command_text and "--post" in command_text


def test_security_agents_never_run_exploit_code_and_the_lead_filters_by_rule():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["security"]]) if k == "agent"}
    lead, lead_body = parse(files["security-lead"])
    assert "edit" not in lead["permission"] and lead["permission"]["spare-gpu_security_filter"] == "allow"
    assert lead["permission"]["spare-gpu_tally_findings"] == "allow" and "`tally_findings` tool" in lead_body and "--plan" in lead_body
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
    for component in ("research", "review", "security", "simplify", "verify"):  # parallel helpers queue together
        _, body = parse({n: p for k, n, p in pack.markdown_files([COMPONENTS[component]]) if k == "agent"}[f"{component}-lead"])
        flat = " ".join(body.split())
        assert "a batch counts once" in flat and "standard-model runs times" not in flat, component
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


def test_every_installed_file_variants_included_parses_and_helpers_stay_hidden():
    for kind, name, text in pack.install_files(pack.select(None), FILLED):
        head = text.split("\n---\n", 1)[0][4:]
        data = yaml.safe_load(head)  # a frontmatter that doesn't parse loses `hidden`: the picker offers the agent
        if kind == "agent" and data["mode"] == "subagent":
            assert data["hidden"] is True, name
    variant = dict(((k, n), t) for k, n, t in pack.install_files(pack.select("review"), FILLED))[("agent", "review-verifier-standard")]
    assert yaml.safe_load(variant.split("\n---\n", 1)[0][4:])["description"].endswith("measured switch-safe)")


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
    for step in ("brief.md", "checklist", "plan.md", "perspectives", "all in the same message", "outline.md",
                 "collect_sources", "sources.json", "verify_refs", "DROP", "spare_waits", "queue time saved",
                 "Never claim a number you didn't measure", "never more than three rounds", "build_report",
                 "No polishing beyond the review", "01-summary.md", "at most three short questions",
                 "the number `pack_cost` counted"):
        assert step in body, step
    assert body.count("all in the same message") == 2  # scouts and writers both run at once
    for kind in ("academic", "technical", "market", "fact"):  # each kind of question has its own strategy
        assert f"| {kind}" in body, kind
    quick = body.split("| quick")[1]  # the depth table's quick row onwards
    assert [c.strip() for c in quick.split("|")[1:6]] == ["3", "1", "4 searches, 6 sources, 500 words", "250 words", "1"]


def test_scouts_rate_sources_stop_in_time_and_list_them_for_collect_sources():
    files = {n: p for k, n, p in pack.markdown_files([COMPONENTS["research"]]) if k == "agent"}
    _, scout = parse(files["research-scout"])
    flat = " ".join(scout.split())
    for part in ("A: primary or authoritative", "B: reputable secondary", "C: weak", "two searches in a row find nothing new",
                 "## In brief", "## Conflicts", "## Gaps", "```sources", '"quality"', '"date"'):
        assert part in flat, part
    # the example lines in the scout's instructions are what collect_sources reads
    from gpu_token_utilization import refs
    items, bad = refs._note_sources(scout)
    assert [i["key"] for i in items] == ["lecun2015", "pytorch-rel-2-5"] and bad == 0
    _, checker = parse(files["research-checker"])
    assert "Coverage" in checker and "brief.md" in checker


def test_the_pack_config_hides_the_research_helpers_and_tools_from_everyone_else():
    doc = pack.config(pack.select("research"), FILLED)
    assert doc["permission"]["task"] == {"research-scout": "deny", "research-writer": "deny", "research-checker": "deny",
                                         "research-checker-standard": "deny"}
    assert (doc["permission"]["spare-gpu_scholar_search"] == doc["permission"]["spare-gpu_verify_refs"]
            == doc["permission"]["spare-gpu_build_report"] == "deny")
    assert doc["mcp"] == {"exa": {"type": "remote", "url": "https://mcp.exa.ai/mcp", "oauth": False}}


def test_every_lead_stops_at_plan_and_every_agent_has_its_step_limit():
    for component in COMPONENTS.values():
        for kind, name, path in pack.markdown_files([component]):
            if kind != "agent":
                continue
            data, body = parse(path)
            if name.endswith("-lead"):
                assert "`--plan`" in body and "stop without starting anything" in " ".join(body.split()), name


def test_the_auto_handoff_agent_may_call_only_autos_two_tools():
    from gpu_token_utilization import bc_config

    text = (bc_config.AGENTS_DIR / "spare-auto.md").read_text(encoding="utf-8")
    assert text.startswith("---\n" + bc_config.COMMAND_MARKER)
    data = yaml.safe_load(text.split("\n---\n", 1)[0][4:])
    assert data["mode"] == "primary" and data["hidden"] is True and data["steps"] <= 4
    allowed = {tool for tool, action in data["permission"].items() if action == "allow"}
    assert data["permission"]["*"] == "deny"
    assert allowed == {"spare-gpu_spare_auto", "spare-gpu_spare_waits"}
    auto = (bc_config.COMMANDS_DIR / "auto.md").read_text(encoding="utf-8")
    assert "\nagent: spare-auto\n" in auto
    # every tool auto.md tells the model to call is one the agent may call
    assert set(re.findall(r"`(spare_\w+)`", auto)) == {t.removeprefix("spare-gpu_") for t in allowed}
