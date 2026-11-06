"""`gtu pack-check`: the parts of the pack's manual checks that can be read from files, as a
doctor. It never starts BharatCode and never changes anything; it says what is installed, what
is stale, what is missing, and what conflicts with the user's own settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .. import bc_config, router, waits
from . import COMPONENTS, HEAVY, STANDARD, SWITCHABLE, Models, config, frontmatter, helpers, install_files, select

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Finding:
    status: str
    text: str

    def __str__(self) -> str:
        return f"{self.status}: {self.text}"


def _marked(path: Path) -> bool:
    try:
        return bc_config.COMMAND_MARKER in path.read_text(encoding="utf-8-sig")
    except OSError:
        return False


def installed_components(folder: Path) -> list[str]:
    """Which components' command files carry spare-gpu's marker in this config folder (the split
    is in when the pack's config file sets small_model)."""
    names = []
    for component in COMPONENTS.values():
        if component.commands:
            if all(_marked(folder / "command" / f"{name}.md") for name in component.commands):
                names.append(component.name)
    return names


def installed_models(folder: Path) -> Models | None:
    """The model ids the installed files carry: from a heavy lead and a standard helper."""
    heavy = standard = None
    for component in COMPONENTS.values():
        for name in component.agents:
            data = frontmatter((folder / "agent" / f"{name}.md").read_text(encoding="utf-8-sig")) \
                if (folder / "agent" / f"{name}.md").is_file() else {}
            model = data.get("model")
            if not model:
                continue
            if name.endswith("-lead") and name != "context-lead" and heavy is None:
                heavy = model
            if (name.endswith(("-scout", "-finder", "-writer", "-driver")) or name == "context-lead") and standard is None:
                standard = model
    return Models(heavy, standard) if heavy and standard else None


def run(folder: Path, how: str = "") -> list[Finding]:
    found: list[Finding] = []
    user = bc_config.config_file(folder)
    found.append(Finding(PASS if user.is_file() else FAIL,
                         f"your config file: {user}" + (f" ({how})" if how else "") + ("" if user.is_file() else " is missing; run `gtu integrate`")))
    try:
        user_config = bc_config.parse_jsonc(user.read_text(encoding="utf-8-sig")) if user.is_file() else {}
        if user.is_file():
            found.append(Finding(PASS, "it parses"))
    except ValueError as exc:
        user_config = {}
        found.append(Finding(FAIL, f"it doesn't parse ({exc}); BharatCode would ignore it"))
    entry = (user_config.get("mcp") or {}).get(bc_config.SERVER_NAME) or {}
    if not entry:
        found.append(Finding(FAIL, "spare-gpu's MCP server isn't registered; run `gtu integrate --pack`"))
    elif bc_config.PACK_FLAG not in (entry.get("command") or []):
        found.append(Finding(FAIL, "spare-gpu's MCP server starts without --pack, so the pack's tools don't exist; run `gtu integrate --pack`"))
    else:
        found.append(Finding(PASS, "spare-gpu's MCP server starts with --pack"))

    pack_files = [folder / n for n in bc_config.PACK_NAMES if bc_config.is_pack_file(folder / n)]
    if not pack_files:
        found.append(Finding(FAIL, "the pack's settings file is missing; run `gtu integrate --pack`"))
        return found
    pack_file = pack_files[0]
    try:
        doc = bc_config.parse_jsonc(pack_file.read_text(encoding="utf-8-sig"))
        found.append(Finding(PASS, f"the pack's settings file {pack_file.name} parses"))
    except ValueError as exc:
        found.append(Finding(FAIL, f"the pack's settings file {pack_file.name} doesn't parse ({exc}); run `gtu integrate --pack`"))
        return found

    names = installed_components(folder)
    if "small_model" in doc:
        names = ["split", *names]
    found.append(Finding(PASS if names else WARN, "installed components: " + (", ".join(names) or "none (only the settings file)")))
    components = select(",".join(names)) if names else []

    # files: present, marked, filled in, parseable
    missing, unmarked, unfilled = [], [], []
    if components:
        # what the installer would write for these components, variants included
        expected = [(kind, name) for kind, name, _ in install_files(components, Models("bharatcode/h", "bharatcode/s"))]
        for kind, name in expected:
            path = folder / kind / f"{name}.md"
            if not path.is_file():
                missing.append(f"{kind}/{name}.md")
                continue
            text = path.read_text(encoding="utf-8-sig")
            if bc_config.COMMAND_MARKER not in text:
                unmarked.append(f"{kind}/{name}.md")
            if HEAVY in text or STANDARD in text or not frontmatter(text).get("description"):
                unfilled.append(f"{kind}/{name}.md")
    found.append(Finding(FAIL if missing else PASS, "files: " + (f"missing {', '.join(missing)}; run `gtu integrate --pack`" if missing
                                                                 else "every command and agent of the installed components is there")))
    if unmarked:
        found.append(Finding(WARN, f"edited by you (marker removed), so updates leave them alone: {', '.join(unmarked)}"))
    if unfilled:
        found.append(Finding(FAIL, f"not filled in or not parseable: {', '.join(unfilled)}; run `gtu integrate --pack`"))

    # models in the files
    models = installed_models(folder)
    if models:
        bad = [m for m in (models.heavy, models.standard) if not m.startswith("bharatcode/")]
        found.append(Finding(FAIL if bad else PASS, f"models in the files: heavy {models.heavy}, standard {models.standard}"
                             + (" (BharatCode's policy allows only bharatcode/ models)" if bad else "")))
        configured = [m.strip() for m in os.environ.get(waits.MODELS_ENV, "").split(",") if m.strip()]
        if len(configured) >= 2 and (waits.model_id(models.heavy), waits.model_id(models.standard)) != \
                (waits.model_id(configured[0]), waits.model_id(configured[1])):
            found.append(Finding(WARN, f"GTU_MODELS says {configured[0]},{configured[1]} but the files carry other ids; run `gtu integrate --pack`"))
    elif components:
        found.append(Finding(WARN, "couldn't read the model ids from the installed agent files"))

    # the settings file says what the installed set needs
    if components and models:
        expected = config(components, models)
        problems = [key for key in ("small_model", "agent", "mcp") if expected.get(key) != doc.get(key)]
        actual_permission = doc.get("permission") or {}
        for key, value in (expected.get("permission") or {}).items():  # extra denies are fine; missing ones aren't
            if key == "task":
                continue  # reported below, helper by helper
            if actual_permission.get(key) != value:
                problems.append(f"permission.{key}")
        if problems:
            found.append(Finding(FAIL, f"the settings file is stale or edited ({', '.join(problems)} differ); run `gtu integrate --pack`"))
        else:
            found.append(Finding(PASS, "the settings file matches the installed components (helpers and tools denied, models, MCP)"))
        task = (doc.get("permission") or {}).get("task") or {}
        loose = [h for h in helpers(components) if task.get(h) != "deny"]
        if loose:
            found.append(Finding(FAIL, f"helpers not denied for your other agents (they cost tokens on every step): {', '.join(loose)}"))

    for conflict in bc_config.pack_conflicts(folder, doc):
        found.append(Finding(WARN, f"conflict: {conflict}"))

    policy = router.load_policy()  # the shipped policy and GTU_SWITCH_SAFE; a project's .gtu/switch-safe counts only in that project
    unmeasured = [r for r in SWITCHABLE if not router.switch_safe(r, policy)]
    found.append(Finding(PASS, "switchable roles on the heavy model until measured: " + (", ".join(unmeasured) or "none (all switch-safe)")
                         + " (a project's .gtu/switch-safe can add to this)"))
    return found


def check_channels() -> list[Finding]:
    """Other Desktop channels that have a config folder but no pack."""
    found = []
    for channel in bc_config.installed_channels():
        if channel == "stable":
            continue
        folder = bc_config.channel_config_dir(channel, ask_cli=False)
        if not any(bc_config.is_pack_file(folder / n) for n in bc_config.PACK_NAMES):
            found.append(Finding(WARN, f"BharatCode Desktop's {channel} channel has a config folder without the pack: "
                                       f"`gtu integrate --pack --channel {channel}`"))
    return found


def worst(findings: list[Finding]) -> str:
    return FAIL if any(f.status == FAIL for f in findings) else WARN if any(f.status == WARN for f in findings) else PASS
