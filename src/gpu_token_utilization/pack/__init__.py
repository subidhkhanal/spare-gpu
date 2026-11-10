"""The spare-gpu pack for BharatCode Desktop: commands that split each job between the heavy and
the standard model (DeepSeek plans, judges and verifies; Qwen does the many quick, parallel,
self-contained steps), built only from BharatCode's own extension points: Markdown commands and
agents, tools of spare-gpu's MCP server, and a config file of the pack's own.

Installed by `gtu integrate --pack`; `gtu integrate --pack --uninstall` removes exactly what it
added. For BharatCode; not affiliated with BharatCode or Anthropic.

Model ids are never hard-coded here: the Markdown carries {{HEAVY_MODEL}} and {{STANDARD_MODEL}},
filled in at install time from bharatcode.ai's model list (or GTU_MODELS).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import api, waits

PACK_DIR = Path(__file__).parent
MARKER = "# Installed by spare-gpu"  # in a Markdown file's frontmatter: ours, until the user removes it
HEAVY, STANDARD = "{{HEAVY_MODEL}}", "{{STANDARD_MODEL}}"
SCHEMA = "https://opencode.ai/config.json"
# Web search for /deep-research: queries go to Exa. Keyless, so OAuth auto-detection is off, as the
# marketplace's Cloudflare-docs entry does it.
EXA_MCP = {"type": "remote", "url": "https://mcp.exa.ai/mcp", "oauth": False}
# How many tools each MCP server the pack adds exposes (Desktop listed exa_web_search_exa and
# exa_web_fetch_exa, 7 Nov 2026). Their tools are hidden from every agent but the pack's own.
MCP_TOOL_COUNT = {"exa": 2}
SERVER = "spare-gpu"  # the MCP server the pack's tools live in; BharatCode names its tools <server>_<tool>
# Heavy-model roles that also ship as a standard-model variant (<name>-standard). The lead starts
# the variant the spare_waits tool names: the router moves a role down only when the role is
# measured switch-safe in routing_policy.json and the heavy model's wait is longer.
SWITCHABLE = ("research-checker", "review-verifier", "security-verifier")
VARIANT_SUFFIX = "-standard"
SHARED_TOOLS = ("pack_cost",)  # pack-only tools every lead uses
# Every pack agent carries a step limit Desktop enforces (`steps` in its frontmatter), so a
# confused helper stops instead of looping: leads orchestrate and may need more; helpers do one job.
LEAD_STEPS = 60
HELPER_STEPS = 30
# Files an earlier version installed and this one no longer has: removed on install and uninstall.
RETIRED = (("command", "context-report"), ("agent", "context-lead"))
# Memory across chats: a short file every agent reads at the start of each request (Desktop's
# `instructions` config, resolved from the project folder up), plus a hand-off file one chat
# writes and the next reads. The memory file costs its size on every step, hence the cap.
MEMORY_FILE = ".gtu/memory.md"
HANDOFF_FILE = ".gtu/handoff.md"
MEMORY_MAX_CHARS = 4_000  # about a thousand tokens on every step in that project


@dataclass(frozen=True)
class Component:
    name: str
    what: str
    commands: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()  # pack-only tools of spare-gpu's MCP server, hidden from every other agent
    mcp: dict = field(default_factory=dict)  # MCP servers the component needs


COMPONENTS: dict[str, Component] = {
    "split": Component("split", "titles and the explore subagent on the standard model"),
    "research": Component("research", "/deep-research", ("deep-research",),
                          ("research-lead", "research-scout", "research-writer", "research-checker"),
                          ("scholar_search", "verify_refs", "collect_sources", "build_report", *SHARED_TOOLS), {"exa": EXA_MCP}),
    "review": Component("review", "/code-review", ("code-review",),
                        ("review-lead", "review-finder", "review-verifier"), ("tally_findings", *SHARED_TOOLS)),
    "security": Component("security", "/security-review", ("security-review",),
                          ("security-lead", "security-finder", "security-verifier"),
                          ("security_filter", "tally_findings", *SHARED_TOOLS)),
    "simplify": Component("simplify", "/simplify", ("simplify",), ("simplify-lead", "simplify-finder"), SHARED_TOOLS),
    # Needs the Playwright MCP from BharatCode's marketplace (server name "playwright"); the pack adds no server for it.
    "verify": Component("verify", "/verify-app", ("verify-app",), ("verify-lead", "verify-driver"), SHARED_TOOLS),
    # No agents of its own: the commands run in the user's current agent, which has the chat's history.
    "memory": Component("memory", "/remember, /handoff and /pickup", ("remember", "handoff", "pickup")),
    # spare-gpu's own chat commands: one small agent on the standard model calls the one tool each names.
    "manage": Component("manage", "/apply, /undo, /cost, /prompts and /doctor",
                        ("apply", "undo", "cost", "prompts", "doctor"), ("spare-helper",),
                        ("apply_result", "undo_result", "cost_summary", "prompt_rules", "doctor_check")),
}


class PackError(Exception):
    """A request about the pack was refused; the message says why."""


def select(only: str | None = None) -> list[Component]:
    """The components named in `only` (comma-separated), or all of them."""
    if not only or not only.strip():
        return list(COMPONENTS.values())
    chosen = []
    for name in (n.strip() for n in only.split(",")):
        if name not in COMPONENTS:
            raise PackError(f"unknown pack component {name!r}; the components are {', '.join(COMPONENTS)}")
        if COMPONENTS[name] not in chosen:
            chosen.append(COMPONENTS[name])
    return chosen


@dataclass(frozen=True)
class Models:
    heavy: str  # full ids, bharatcode/...
    standard: str

    def fill(self, text: str) -> str:
        return text.replace(HEAVY, self.heavy).replace(STANDARD, self.standard)


def resolve_models(client=None, lite: bool = False) -> tuple[Models, str]:
    """The heavy and the standard model, from bharatcode.ai's public model list (access tier
    "heavy" / "standard", else by family name), or GTU_MODELS (heavy first). Returns a note when
    the known ids had to be used instead. `lite` (a Qwen-only plan) puts every role on the standard model."""
    note = ""
    configured = [m.strip() for m in os.environ.get(waits.MODELS_ENV, "").split(",") if m.strip()]
    if len(configured) >= 2:
        heavy, standard = waits.model_id(configured[0]), waits.model_id(configured[1])
    else:
        heavy = standard = None
        try:
            listed = api.public_models(client=client)
        except api.ApiError as exc:
            note = f" (couldn't read bharatcode.ai's model list: {exc}; using the known ids)"
        else:
            heavy = _pick(listed, "heavy", "deepseek")
            standard = _pick(listed, "standard", "qwen")
            if not heavy or not standard:
                note = " (bharatcode.ai's model list didn't name both models; using the known ids)"
        if not heavy or not standard:
            heavy, standard = waits.DEFAULT_MODELS
    if lite:
        heavy = standard
    return Models(f"bharatcode/{heavy}", f"bharatcode/{standard}"), note


def _pick(models, tier: str, family: str) -> str | None:
    for m in models:
        if (m.access_tier or "").lower() == tier:
            return m.id
    for m in models:
        if family in m.id.lower():
            return m.id
    return None


# --- the pack's Markdown -----------------------------------------------------------------------

def frontmatter(text: str) -> dict[str, str]:
    """The simple `key: value` lines of a Markdown file's frontmatter (comment lines skipped)."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("\"'")
    return data


def markdown_files(components, source: Path = PACK_DIR) -> list[tuple[str, str, Path]]:
    """(kind, name, path) for each command and agent file of the components that exists in `source`."""
    found = []
    for component in components:
        for kind, names in (("command", component.commands), ("agent", component.agents)):
            for name in names:
                path = source / f"{kind}s" / f"{name}.md"
                if path.is_file():
                    found.append((kind, name, path))
    return found


def variant_of(name: str) -> str | None:
    """The standard-model variant's name for a switchable role, else None."""
    return name + VARIANT_SUFFIX if name in SWITCHABLE else None


def helper_names(component: Component) -> list[str]:
    """Every agent of the component that isn't its lead, variants included."""
    lead = {name for name in component.agents if name.endswith("-lead")}
    names = []
    for name in component.agents:
        if name in lead:
            continue
        names.append(name)
        if variant_of(name):
            names.append(variant_of(name))
    return names


def all_markdown_names() -> list[tuple[str, str]]:
    """(kind, name) of every file the pack can install, variants included, whichever components exist yet."""
    out = []
    for c in COMPONENTS.values():
        out += [("command", name) for name in c.commands]
        for name in c.agents:
            out.append(("agent", name))
            if variant_of(name):
                out.append(("agent", variant_of(name)))
    return out + list(RETIRED)


def helpers(components, source: Path = PACK_DIR) -> list[str]:
    """The pack's subagents: every agent file whose mode isn't primary, and the standard-model
    variants. BharatCode describes every subagent to every agent on every step, so these are
    denied globally and allowed only inside the pack's lead agents."""
    names = []
    for kind, name, path in markdown_files(components, source):
        if kind == "agent" and frontmatter(path.read_text(encoding="utf-8")).get("mode") != "primary":
            names.append(name)
            if variant_of(name):
                names.append(variant_of(name))
    return names


def variant_text(raw: str) -> str:
    """A switchable agent's file as its standard-model variant: the model placeholder swapped, and
    the description saying so. Made at install time from the one source file, so the two can't drift."""
    text = raw.replace(HEAVY, STANDARD)
    # No ": " in the added words: the description is a plain YAML value, and a colon there breaks the
    # frontmatter, so BharatCode would miss `hidden` and offer the variant in the agent picker.
    return re.sub(r"^(description: .*)$", r"\1 (standard-model variant, started when the heavy model is queued and "
                  "the role is measured switch-safe)", text, count=1, flags=re.M)


def install_files(components, models: Models, source: Path = PACK_DIR) -> list[tuple[str, str, str]]:
    """(kind, name, text) of everything to write into the config folder: each command and agent
    with the model ids filled in, plus the standard-model variant of each switchable agent."""
    out = []
    for kind, name, path in markdown_files(components, source):
        raw = path.read_text(encoding="utf-8")
        out.append((kind, name, models.fill(raw)))
        if kind == "agent" and variant_of(name):
            out.append((kind, variant_of(name), models.fill(variant_text(raw))))
    return out


# --- the pack's config file ---------------------------------------------------------------------

def config(components, models: Models, source: Path = PACK_DIR) -> dict:
    """What the pack's config file says: the model split, the pack's helpers and tools hidden from
    every other agent, and the MCP servers its commands need."""
    doc: dict = {"$schema": SCHEMA}
    names = {c.name for c in components}
    if "split" in names:
        # compaction and general stay on the session model: compaction reads a whole history, and
        # general writes code. Titles and explore are short, read-only jobs.
        doc["small_model"] = models.standard
        doc["agent"] = {"title": {"model": models.standard}, "explore": {"model": models.standard}}
    permission: dict = {}
    task = {name: "deny" for name in helpers(components, source)}
    if task:
        permission["task"] = task
    for component in components:
        for tool in component.tools:
            permission[f"{SERVER}_{tool}"] = "deny"  # BharatCode then leaves the tool out of every other agent's request
    mcp = {}
    for component in components:
        mcp.update(component.mcp)
    for server in mcp:
        permission[f"{server}_*"] = "deny"  # its tools, too; the agent that needs them allows them again
    if permission:
        doc["permission"] = permission
    if mcp:
        doc["mcp"] = mcp
    memory = [c for c in components if c.name == "memory"]
    if memory and markdown_files(memory, source):  # only when its command files exist to be installed
        doc["instructions"] = [MEMORY_FILE]  # a relative glob: Desktop looks for it from the project folder up
    return doc


# Measured 31 Oct 2026 (research/token-overhead.md): what one MCP tool's schema and one line of
# the task tool's subagent list cost on every step.
TOKENS_PER_MCP_TOOL = 330
TOKENS_PER_SUBAGENT_LINE = 40


def pack_added_tokens(doc: dict, helper_list: list[str] | None = None) -> int:
    """What the pack adds to a normal session's request with this pack config: every pack tool
    and helper agent the config fails to deny is described on every step."""
    permission = doc.get("permission") or {}
    tools = [t for c in COMPONENTS.values() for t in c.tools if permission.get(f"{SERVER}_{t}") != "deny"]
    task = permission.get("task") or {}
    names = helper_list if helper_list is not None else helpers(COMPONENTS.values())
    visible = [h for h in names if task.get(h) != "deny"]
    added_servers = sum(MCP_TOOL_COUNT.get(server, 1) for server in doc.get("mcp") or {}
                        if permission.get(f"{server}_*") != "deny")
    return (TOKENS_PER_MCP_TOOL * (len(set(tools)) + added_servers)  # a shared tool is described once
            + TOKENS_PER_SUBAGENT_LINE * len(visible))


def config_text(doc: dict, marker_line: str) -> str:
    """The config file's text: the marker comment first (BharatCode reads JSON with comments)."""
    return "{\n  " + marker_line + json.dumps(doc, indent=2)[1:] + "\n"


def describe(doc: dict) -> str:
    """One line on what the config file does, for the user."""
    parts = []
    if "small_model" in doc:
        parts.append(f"titles and explore on {waits.name(doc['small_model'])}")
    task = doc.get("permission", {}).get("task", {})
    if task:
        parts.append(f"{len(task)} helper agent(s) hidden from your own agents")
    tools = [k for k in doc.get("permission", {}) if k != "task" and not k.endswith("_*")]
    if tools:
        parts.append(f"{len(tools)} pack-only tool(s) hidden from them too")
    if doc.get("mcp"):
        parts.append("MCP: " + ", ".join(doc["mcp"]) + " (its tools only for the pack's agents)")
    if doc.get("instructions"):
        parts.append(f"{doc['instructions'][0]} read by every agent")
    return "; ".join(parts) or "nothing yet"
