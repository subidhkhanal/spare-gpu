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
SERVER = "spare-gpu"  # the MCP server the pack's tools live in; BharatCode names its tools <server>_<tool>


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
                          ("scholar_search", "verify_refs"), {"exa": EXA_MCP}),
    "review": Component("review", "/code-review", ("code-review",),
                        ("review-lead", "review-finder", "review-verifier")),
    "security": Component("security", "/security-review", ("security-review",),
                          ("security-lead", "security-finder", "security-verifier"), ("security_filter",)),
    "simplify": Component("simplify", "/simplify", ("simplify",), ("simplify-lead", "simplify-finder")),
    "context": Component("context", "/context-report", ("context-report",), ("context-lead",), ("context_report",)),
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


def all_markdown_names() -> list[tuple[str, str]]:
    """(kind, name) of every file the pack can install, whichever components exist yet."""
    return [(kind, name) for c in COMPONENTS.values() for kind, names in (("command", c.commands), ("agent", c.agents))
            for name in names]


def helpers(components, source: Path = PACK_DIR) -> list[str]:
    """The pack's subagents: every agent file whose mode isn't primary. BharatCode describes
    every subagent to every agent on every step, so these are denied globally and allowed only
    inside the pack's lead agents."""
    names = []
    for kind, name, path in markdown_files(components, source):
        if kind == "agent" and frontmatter(path.read_text(encoding="utf-8")).get("mode") != "primary":
            names.append(name)
    return names


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
    if permission:
        doc["permission"] = permission
    mcp = {}
    for component in components:
        mcp.update(component.mcp)
    if mcp:
        doc["mcp"] = mcp
    return doc


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
    tools = [k for k in doc.get("permission", {}) if k != "task"]
    if tools:
        parts.append(f"{len(tools)} pack-only tool(s) hidden from them too")
    if doc.get("mcp"):
        parts.append("MCP: " + ", ".join(doc["mcp"]))
    return "; ".join(parts) or "nothing yet"
