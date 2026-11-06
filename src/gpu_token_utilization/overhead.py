"""What a BharatCode session pays on every step in this setup, and where it could save: the
numbers behind /context-report and `gtu context-report`.

The counts come from the config and the files on disk; the cost of each kind of item is what
research/token-overhead.md measured (one tiny `bharatcode run` per variant). A live measurement
of the whole overhead is optional (`measure`), because it costs a model call. Writing the
summary in words is the agent's job; nothing here is estimated by a model.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import bc_config, pack
from .agent_cli import CliNotFound, resolve_command
from .api import env_without_key

# Measured 31 Oct 2026 on BharatCode CLI 1.15.46 (research/token-overhead.md).
SYSTEM_PROMPT_TOKENS = 2_000
BUILTIN_TOOLS_TOKENS = 6_000
TOKENS_PER_MCP_TOOL = 330  # 2,329 for 7 tools
TOKENS_PER_SKILL = 220  # 3,504 for 16 skills
BYTES_PER_TOKEN = 3.6  # an AGENTS.md of 5,188 bytes cost 1,423 tokens
TOKENS_PER_SUBAGENT_LINE = 40  # a name and a one-line description in the task tool's list
SPARE_GPU_TOOLS = 5  # spare_delegate, spare_ask, spare_waits, spare_work_status, spare_cancel_work
PROMPT = "Reply with the single word OK."
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".opencode/AGENTS.md")
EXTERNAL_SKILL_DIRS = (".claude/skills", ".agents/skills")  # under the home folder and the project
PROJECT_CONFIGS = ("opencode.jsonc", "opencode.json", ".opencode/opencode.jsonc", ".opencode/opencode.json")


@dataclass
class Item:
    name: str
    kind: str  # base, mcp, skills, instructions, pack
    tokens: int | None  # None: unknown (the formula is in `how`)
    how: str  # where the number comes from
    switch: str | None = None  # the exact change that removes it
    apply: dict | None = None  # {"file": ..., "mcp_disable": name} or {"file": ..., "permission": {...}}: what `apply` does


@dataclass
class Report:
    project: Path
    config_files: list[Path]
    items: list[Item]
    measured: dict | None = None
    usage: str | None = None
    notes: list[str] = field(default_factory=list)

    def estimate(self) -> int:
        return sum(i.tokens or 0 for i in self.items)

    def waste(self) -> list[Item]:
        """What can be switched off, biggest first; unknown sizes last."""
        return sorted((i for i in self.items if i.switch), key=lambda i: (i.tokens is None, -(i.tokens or 0)))


def tokens_of(path: Path) -> int:
    try:
        return round(path.stat().st_size / BYTES_PER_TOKEN)
    except OSError:
        return 0


def _load(path: Path) -> dict:
    try:
        return bc_config.parse_jsonc(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _skill_count(root: Path) -> int:
    try:
        return sum(1 for _ in root.rglob("SKILL.md")) if root.is_dir() else 0
    except OSError:
        return 0


def pack_added_tokens(doc: dict, helpers: list[str] | None = None) -> int:
    """What the pack adds to a normal session's request with this pack config: every pack tool
    and helper agent the config fails to deny is described on every step."""
    permission = doc.get("permission") or {}
    tools = [t for c in pack.COMPONENTS.values() for t in c.tools if permission.get(f"{pack.SERVER}_{t}") != "deny"]
    task = permission.get("task") or {}
    names = helpers if helpers is not None else pack.helpers(pack.COMPONENTS.values())
    visible = [h for h in names if task.get(h) != "deny"]
    return TOKENS_PER_MCP_TOOL * len(tools) + TOKENS_PER_SUBAGENT_LINE * len(visible)


def inventory(project: Path, home: Path | None = None) -> Report:
    """Everything in this setup that a request carries on every step, with its cost."""
    project = Path(project).resolve()
    home = Path(home) if home else Path.home()
    folder = bc_config.channel_config_dir("stable", ask_cli=False)
    user_file = bc_config.config_file(folder)
    files = [user_file] + [folder / n for n in bc_config.PACK_NAMES if bc_config.is_pack_file(folder / n)]
    files += [project / n for n in PROJECT_CONFIGS if (project / n).is_file()]
    configs = {f: _load(f) for f in files if f.is_file()}
    items = [Item("BharatCode's system prompt and built-in tools", "base", SYSTEM_PROMPT_TOKENS + BUILTIN_TOOLS_TOKENS,
                  "measured: 2.0k prompt, 6.0k tool schemas")]
    notes: list[str] = []

    # MCP servers: the last file to set an entry wins, as BharatCode merges them
    servers: dict[str, tuple[dict, Path]] = {}
    for f, cfg in configs.items():
        for name, entry in (cfg.get("mcp") or {}).items():
            if isinstance(entry, dict):
                servers[name] = ({**servers.get(name, ({}, f))[0], **entry}, f)
    for name, (entry, f) in servers.items():
        if entry.get("enabled") is False:
            continue
        count = SPARE_GPU_TOOLS if name == bc_config.SERVER_NAME else None
        if bc_config.is_pack_file(f):  # the pack's own entry: its file isn't edited, the pack is reinstalled without it
            others = ", ".join(c.name for c in pack.COMPONENTS.values() if name not in c.mcp)
            switch, applies = f"it comes with the pack: `gtu integrate --pack --only {others}` reinstalls the pack without it", None
        else:
            switch = f'in {f}: "mcp": {{"{name}": {{"enabled": false}}}}' + (" (then /auto stops working)" if name == bc_config.SERVER_NAME else "")
            applies = {"file": str(f), "mcp_disable": name}
        items.append(Item(
            f"MCP server {name} ({entry.get('type', '?')})", "mcp",
            TOKENS_PER_MCP_TOOL * count if count else None,
            f"{count} tools x {TOKENS_PER_MCP_TOOL} tokens (measured)" if count
            else f"its tool count isn't known here: {TOKENS_PER_MCP_TOOL} tokens per tool; `bharatcode mcp list` names them",
            switch=switch, apply=applies,
        ))

    # Skills: listed in every request unless the skill permission is denied
    denied = any((cfg.get("permission") or {}).get("skill") == "deny" for cfg in configs.values())
    external = sum(_skill_count(home / d) for d in EXTERNAL_SKILL_DIRS) + sum(_skill_count(project / d) for d in EXTERNAL_SKILL_DIRS)
    own = sum(_skill_count(folder / d) for d in ("skill", "skills")) + _skill_count(project / ".opencode" / "skills") \
        + _skill_count(project / ".opencode" / "skill")
    if external + own:
        items.append(Item(
            f"{external + own} skill(s) ({external} from ~/.claude/skills and ~/.agents/skills, {own} from BharatCode's folders)",
            "skills", 0 if denied else TOKENS_PER_SKILL * (external + own),
            "already denied: the list isn't sent" if denied else f"{TOKENS_PER_SKILL} tokens per skill (measured: 3,504 for 16)",
            switch=None if denied else
            f'in {user_file}: "permission": {{"skill": "deny"}} drops the list from every request (the skill tool with it); '
            "OPENCODE_DISABLE_EXTERNAL_SKILLS=1 does the same for the CLI's external folders only",
            apply=None if denied else {"file": str(user_file), "permission": {"skill": "deny"}},
        ))

    # Instruction files go in whole
    for name in INSTRUCTION_FILES:
        f = project / name
        if f.is_file():
            items.append(Item(f"{name} ({f.stat().st_size:,} bytes)", "instructions", tokens_of(f),
                              f"its size / {BYTES_PER_TOKEN} (measured)", switch=f"shorten {f}; every byte of it is sent on every step"))
    for f, cfg in configs.items():
        for entry in cfg.get("instructions") or []:
            path = (project / entry) if not Path(entry).is_absolute() else Path(entry)
            if path.is_file():
                items.append(Item(f"instructions file {entry} ({path.stat().st_size:,} bytes)", "instructions", tokens_of(path),
                                  f"its size / {BYTES_PER_TOKEN} (measured)", switch=f"shorten {path}, or drop it from \"instructions\" in {f}"))

    # The pack
    pack_docs = [cfg for f, cfg in configs.items() if bc_config.is_pack_file(f)]
    if pack_docs:
        added = pack_added_tokens(pack_docs[0])
        items.append(Item("spare-gpu's command pack", "pack", added,
                          "0 by design: its helpers and tools are denied for every agent but its own" if added == 0
                          else f"{added} tokens: something in its config no longer denies a tool or helper",
                          switch=None if added == 0 else "run `gtu integrate --pack` again to restore its config"))
    else:
        notes.append("The command pack isn't installed here (gtu integrate --pack).")
    return Report(project, list(configs), items, notes=notes)


# --- live measurement and usage (optional: each costs a CLI run) ---------------------------------

def measure(project: Path, command: list[str] | None = None, timeout: float = 300.0) -> dict | None:
    """One tiny `bharatcode run`: the context it sent before the prompt is the real per-step
    overhead. None when the CLI isn't there or the run didn't report its tokens."""
    try:
        argv = command or resolve_command()
    except CliNotFound:
        return None
    try:
        proc = subprocess.run([*argv, "run", "--dir", str(project), "--format", "json", PROMPT], cwd=project,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                              env=env_without_key())
    except (OSError, subprocess.TimeoutExpired):
        return None
    steps = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "step_finish":
            t = (event.get("part") or {}).get("tokens") or {}
            cache = t.get("cache") or {}
            steps.append({"input": t.get("input", 0), "cache_read": cache.get("read", 0), "cache_write": cache.get("write", 0),
                          "output": t.get("output", 0)})
    if not steps or proc.returncode != 0:
        return None
    first = steps[0]
    return {"context": first["input"] + first["cache_read"] + first["cache_write"], "output": first["output"], "steps": len(steps)}


def usage(days: int = 7, command: list[str] | None = None, timeout: float = 60.0) -> str | None:
    """`bharatcode stats --days N --models`, as text; None when the CLI isn't there or has no stats."""
    try:
        argv = command or resolve_command()
    except CliNotFound:
        return None
    try:
        proc = subprocess.run([*argv, "stats", "--days", str(days), "--models"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                              env=env_without_key())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout).strip()
    return text or None


# --- applying a switch ---------------------------------------------------------------------------

def apply(item: Item, now: datetime | None = None) -> Path:
    """Make the item's config change, after a timestamped backup of the file. Returns the backup.
    The file is rewritten as plain JSON (comments don't survive; the backup has them)."""
    if not item.apply:
        raise ValueError("this one is a change to make by hand")
    path = Path(item.apply["file"])
    original = path.read_text(encoding="utf-8-sig") if path.exists() else "{}"
    config = bc_config.parse_jsonc(original)
    if "mcp_disable" in item.apply:
        entry = config.setdefault("mcp", {}).setdefault(item.apply["mcp_disable"], {})
        entry["enabled"] = False
    for key, value in (item.apply.get("permission") or {}).items():
        config.setdefault("permission", {})[key] = value
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}{bc_config.BACKUP_SUFFIX}")
    backup.write_text(original, encoding="utf-8")
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return backup


# --- the text ------------------------------------------------------------------------------------

def describe(report: Report) -> str:
    lines = [f"Per-step overhead in {report.project} (what every request carries before your words):"]
    for item in report.items:
        shown = f"{item.tokens:,}" if item.tokens is not None else "?"
        lines.append(f"- {shown:>7} tokens  {item.name}: {item.how}")
    unknown = [i for i in report.items if i.tokens is None]
    lines.append(f"Estimate: about {report.estimate():,} tokens per step"
                 + (f", plus {len(unknown)} item(s) of unknown size" if unknown else "") + ".")
    if report.measured:
        lines.append(f"Measured just now with one tiny run: {report.measured['context']:,} tokens of context sent "
                     f"({report.measured['output']} out, {report.measured['steps']} step(s)).")
    waste = report.waste()
    if waste:
        lines.append("Top savings, biggest first (each with its exact switch):")
        for n, item in enumerate(waste, 1):
            shown = f"{item.tokens:,} tokens" if item.tokens is not None else "unknown size"
            lines.append(f"{n}. {item.name}: {shown}. Switch: {item.switch}"
                         + (f" (apply it with `gtu context-report --apply {n}`; it backs the file up first)" if item.apply else ""))
    if report.usage:
        lines.append("Usage (bharatcode stats):")
        lines.append(report.usage)
    for note in report.notes:
        lines.append(note)
    lines.append(f"Config files read: {', '.join(str(f) for f in report.config_files) or 'none'}.")
    return "\n".join(lines)
