"""/doctor: one diagnosis of the whole install, each problem with its fix.

Plain, it starts nothing of BharatCode's and changes nothing: it reads spare-gpu's own state,
BharatCode's config folder, the project's .gtu folder, and starts spare-gpu's own MCP server once,
the way Desktop would, to see that it answers.

`/doctor live` (it talks to Desktop's own server, so it runs from a Desktop chat) also runs one
tiny real request as a Desktop session in an empty folder to measure the per-step
context (the pack's overhead budget), checks that a job's copy leaves build output out and that
your own config file holds none of the pack's settings, and writes the outcome into the
manual-check tables under research/ that these checks decide. The live part costs one short
model call. Which tools and agents Desktop actually shows a chat is checked by hand.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from . import __version__, bc_config, desktop, pack, router, runstate, settings, waits, worktree
from .agent import DEFAULT_MODEL
from .budget import Budgets
from .jobs import JobConfig
from .pack import check
from .pack.check import FAIL, PASS, WARN, Finding
from .store import FINAL_STATUSES, Store

PROMPT = "Reply with the single word OK."
MODEL = DEFAULT_MODEL  # the tiny request is pinned to one model: tokens differ per tokenizer
BUDGET = 1_500  # tokens the pack may add to a normal session's step (plan §7)
SPARE_TOOLS = ("spare_delegate", "spare_waits", "spare_work_status", "spare_cancel_work", "spare_auto")
RESEARCH = Path(__file__).resolve().parents[2] / "research"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    filled: dict[tuple[str, int], str] = field(default_factory=dict)  # (manual-check file, row) -> result text

    def add(self, status: str, text: str) -> None:
        self.findings.append(Finding(status, text))

    @property
    def verdict(self) -> str:
        return check.worst(self.findings)


def research_dir() -> Path | None:
    """The research folder of a checkout (editable install); None in a plain install."""
    return RESEARCH if (RESEARCH / "pack-install" / "manual-check.md").is_file() else None


# --- static checks ------------------------------------------------------------------------------

def check_tools(report: Report) -> None:
    report.add(PASS, f"spare-gpu {__version__} on Python {platform.python_version()} ({sys.executable})")
    git = shutil.which("git")
    if git:
        try:
            version = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            version = "found, but `git --version` didn't answer"
        report.add(PASS, f"git: {version}")
    else:
        report.add(FAIL, "git isn't installed or isn't on PATH; jobs copy your project with git. The installer adds it "
                        "(winget install Git.Git), then open a new terminal.")
    if sys.platform == "win32":
        if worktree._long_paths_enabled():
            report.add(PASS, "Windows long paths are enabled")
        else:
            report.add(WARN, "Windows long paths are off: a job's copy under .gtu\\worktrees can exceed 260 characters and "
                             "fail. Enable it once (as administrator): New-ItemProperty -Path "
                             "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 "
                             "-PropertyType DWord -Force")


def _registered_command(folder: Path) -> tuple[list[str] | None, Finding]:
    user = bc_config.config_file(folder)
    if not user.is_file():
        return None, Finding(FAIL, f"BharatCode's config file {user} is missing; run `gtu integrate`")
    try:
        doc = bc_config.parse_jsonc(user.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        return None, Finding(FAIL, f"BharatCode's config file {user} doesn't parse ({exc}); Desktop ignores a broken file. "
                                   f"Fix it by hand (a backup may be next to it as {user.name}{bc_config.BACKUP_SUFFIX})")
    mcp = doc.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        return None, Finding(FAIL, f'"mcp" in {user.name} isn\'t an object, so Desktop ignores every MCP server; fix it by hand')
    entry = (mcp or {}).get(bc_config.SERVER_NAME)
    if entry is not None and not isinstance(entry, dict):
        return None, Finding(FAIL, f"spare-gpu's MCP entry in {user.name} isn't an object; run `gtu integrate`")
    entry = entry or {}
    command = entry.get("command") if isinstance(entry.get("command"), list) else None
    if not command:
        return None, Finding(FAIL, f"spare-gpu isn't registered as an MCP server in {user.name}; run `gtu integrate`")
    if entry.get("enabled") is False:
        return None, Finding(FAIL, f"spare-gpu's MCP entry in {user.name} is disabled (\"enabled\": false); "
                                   "set it to true or run `gtu integrate`")
    exe = command[0]
    if not (Path(exe).is_file() or shutil.which(exe)):
        return None, Finding(FAIL, f"the registered command {exe} doesn't exist, so Desktop can't start spare-gpu; "
                                   "run `gtu integrate` (it registers this install's gtu)")
    return command, Finding(PASS, f"spare-gpu is registered in {user.name} and its command exists: {' '.join(command)}")


def check_config(report: Report, folder: Path, how: str) -> list[str] | None:
    if not folder.is_dir():
        report.add(FAIL, f"BharatCode's config folder {folder} doesn't exist ({how}). Install BharatCode Desktop, open it "
                         "once, then run `gtu integrate`.")
        return None
    report.add(PASS, f"BharatCode's config folder: {folder} ({how})")
    command, finding = _registered_command(folder)
    report.findings.append(finding)
    auto = folder / "command" / "auto.md"
    if not auto.is_file():
        report.add(WARN, "the /auto command isn't installed; run `gtu integrate`")
    elif bc_config.COMMAND_MARKER not in auto.read_text(encoding="utf-8-sig", errors="replace"):
        report.add(PASS, "the /auto command is there, edited by you (spare-gpu leaves it alone)")
    elif not (folder / "agent" / "spare-auto.md").is_file():
        report.add(WARN, "the /auto command is installed but its spare-auto agent isn't; run `gtu integrate`")
    else:
        report.add(PASS, "the /auto command is installed")
    return command


def pack_installed(folder: Path) -> bool:
    return any(bc_config.is_pack_file(folder / n) for n in bc_config.PACK_NAMES)


def check_pack(report: Report, folder: Path, how: str, command: list[str] | None) -> None:
    if not pack_installed(folder):
        report.add(WARN, "the command pack isn't installed, so only /auto is there: `gtu integrate --pack` adds /apply, "
                         "/undo, /cost, /prompts, /doctor, /code-review, /security-review, /deep-research, /simplify, "
                         "/verify-app and the memory commands")
        if command and bc_config.PACK_FLAG in command:
            report.add(WARN, "spare-gpu's MCP server starts with --pack although the pack's settings file is gone; "
                             "run `gtu integrate` (or `gtu integrate --pack` to put the pack back)")
        return
    for finding in check.run(folder, how) + check.check_channels():
        report.add(finding.status, "pack: " + finding.text)


def check_server(report: Report, command: list[str] | None, timeout: float = 90.0) -> None:
    """Start spare-gpu's MCP server the way Desktop would and list its tools."""
    if not command:
        return
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="spare-gpu-doctor-") as scratch:
        params = StdioServerParameters(command=command[0], args=[*command[1:], "--dir", scratch],
                                       env={**os.environ, "PYTHONUTF8": "1"})
        log_path = Path(scratch) / "server-stderr.log"

        async def go():
            with anyio.fail_after(timeout), open(log_path, "w", encoding="utf-8", errors="replace") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return sorted(t.name for t in (await session.list_tools()).tools)

        try:
            names = anyio.run(go)
        except Exception as exc:  # noqa: BLE001  whatever stopped it is the finding
            said = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines() if log_path.is_file() else []
            detail = said[-1] if said else (str(exc).strip() or type(exc).__name__)
            report.add(FAIL, f"spare-gpu's MCP server didn't start from `{' '.join(command)}`: {detail}. Run that "
                             "command in a terminal to see its error; `gtu integrate` re-registers this install.")
            return
    missing = [t for t in SPARE_TOOLS if t not in names]
    extra = [t for t in names if t not in SPARE_TOOLS]
    if missing:
        report.add(FAIL, f"spare-gpu's MCP server started but lacks {', '.join(missing)}; reinstall spare-gpu")
    else:
        report.add(PASS, f"spare-gpu's MCP server starts and offers {len(names)} tools"
                         + (f" (the pack's: {', '.join(extra)})" if extra else ""))


def _alive_pid(folder: Path) -> tuple[int | None, bool]:
    info = runstate.read_run(folder)
    pid = info.get("pid") if info else None
    return (pid, runstate.alive(pid)) if isinstance(pid, int) else (None, False)


def check_project(report: Report, cwd: Path) -> Path:
    try:
        root = worktree.repo_root(cwd).resolve()
        kind = "git repo"
    except worktree.GitError:
        root = Path(cwd).resolve()
        kind = ("plain folder (a copy is made for each job; no git history needed)" if shutil.which("git")
                else "folder; without git, whether it's a repo can't be told")
    report.add(PASS, f"project: {root} ({kind})")
    # The same store the MCP server opens from here: GTU_DB, else <project>/.gtu/gtu.db.
    db = Store.default_path(Path(cwd)).resolve()
    folder = db.parent
    if not db.is_file():
        report.add(PASS, f"no jobs have run here yet (no {folder})")
        return root
    try:
        with Store(db) as store:
            jobs = store.list_jobs()
            ids = {j.id: j for j in jobs}
    except Exception as exc:  # noqa: BLE001  a corrupt or locked database is the finding
        report.add(FAIL, f"the job store {db} can't be opened ({type(exc).__name__}: {exc}); move it aside to start fresh")
        return root
    counts = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    shown = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"
    report.add(PASS, f"jobs here: {shown}")
    pid, alive = _alive_pid(folder)
    if pid and alive:
        report.add(PASS, f"a job is running now (process {pid})")
    elif pid:
        report.add(WARN, f"a run file is left from process {pid}, which is gone (Desktop closed or crashed during a "
                         "job); the next job handed over here tidies it and pauses any job left running")
    running = [j.id for j in jobs if j.status == "running"] if not (pid and alive) else []
    if running:
        report.add(WARN, f"job(s) marked running with nothing running them: {', '.join(running)}; the next job "
                         "handed over here pauses them")
    paused = [j.id for j in jobs if j.status == "paused"]
    if paused:
        report.add(PASS, f"job(s) that stopped before finishing (token budget or a stop): {', '.join(paused)}; "
                         "ask for the task again to run it afresh")
    copies = folder / "worktrees"
    orphans = [p.name for p in copies.iterdir() if p.is_dir() and (p.name not in ids or ids[p.name].status in FINAL_STATUSES)] \
        if copies.is_dir() else []
    if orphans:
        report.add(WARN, f"leftover job copies with no unfinished job: {', '.join(orphans)} in {copies}; delete them "
                         "(nothing depends on them)")
    return root


def check_settings(report: Report, root: Path) -> None:
    try:
        found = settings.load(root)
        config, budgets = JobConfig.load(root), Budgets.load(root)
    except ValueError as exc:
        report.add(FAIL, f"job settings: {exc}")
    else:
        overridden = [key for key, (env, _) in settings.KEYS.items() if os.environ.get(env)]
        where = (f"settings file {found.path}" if found.present
                 else f"defaults (no settings file; {settings.SETTINGS_NAME} in the project's .gtu folder changes them)")
        report.add(PASS, f"job settings from {where}: test command {' '.join(config.test_command)!r}, "
                         f"{budgets.per_job:,} tokens per job"
                         + (f"; overridden by the environment: {', '.join(overridden)}" if overridden else ""))
    policy = router.load_policy(root / ".gtu")
    safe = sorted(k for k, v in (policy.get("types") or {}).items() if v == "switch-safe")
    report.add(PASS, "job types that may move to the faster model: " + (", ".join(safe) or "none")
                     + f" (routing_policy.json plus {root / '.gtu' / router.SWITCH_SAFE_FILE})")


def check_memory(report: Report, root: Path, folder: Path) -> None:
    """The project's memory file: read by every agent on every request when the pack's memory
    component is installed, so its size is a per-step cost."""
    memory = root / pack.MEMORY_FILE
    installed = folder.is_dir() and pack_installed(folder) and "memory" in check.installed_components(folder)
    if not memory.is_file():
        if installed:
            report.add(PASS, "no project memory yet; /remember writes .gtu/memory.md")
        return
    text = memory.read_text(encoding="utf-8", errors="replace")
    size, lines = len(text), len(text.splitlines())
    cost = f"about {size // 4:,} tokens on every step in this project"
    if not installed:
        report.add(WARN, f"project memory .gtu/memory.md exists but the pack's memory component isn't installed, so no agent "
                         "reads it; `gtu integrate --pack --only memory` (or the whole pack) turns it on")
    elif size > pack.MEMORY_MAX_CHARS:
        report.add(WARN, f"project memory .gtu/memory.md is {size:,} characters ({lines} lines), over the {pack.MEMORY_MAX_CHARS:,} cap: "
                         f"{cost}. Trim it (/remember with no note shows it)")
    else:
        report.add(PASS, f"project memory .gtu/memory.md: {lines} lines, {size:,} characters ({cost})")


# --- live checks ---------------------------------------------------------------------------------

def expected_pack(folder: Path) -> tuple[list[str], list[str], dict]:
    """(helper agents, pack-only tool names, the pack's settings) for the installed components."""
    names = check.installed_components(folder)
    pack_file = next(folder / n for n in bc_config.PACK_NAMES if bc_config.is_pack_file(folder / n))
    doc = bc_config.parse_jsonc(pack_file.read_text(encoding="utf-8-sig"))
    if "small_model" in doc:
        names = ["split", *names]
    components = pack.select(",".join(names)) if names else []
    tools = sorted({f"{pack.SERVER}_{t}" for c in components for t in c.tools})
    return pack.helpers(components), tools, doc


def check_own_file(report: Report, folder: Path) -> None:
    """With the pack installed, your own config file should hold none of the pack's settings: the
    pack keeps them in a file of its own (manual-check pack-install row 2)."""
    if not pack_installed(folder):
        return
    try:
        _, _, doc = expected_pack(folder)
    except ValueError as exc:
        report.add(FAIL, f"live: the pack's settings file doesn't parse ({exc}); run `gtu integrate --pack`")
        return
    user_file = bc_config.config_file(folder)
    try:
        user_doc = bc_config.parse_jsonc(user_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return  # the static check already named the missing or broken file; row 2 stays undecided
    leaked = []
    for keys, value in bc_config._leaves(doc):  # the pack's own settings, leaf by leaf
        current = user_doc
        for key in keys:
            current = current.get(key) if isinstance(current, dict) else None
        if current is not None and current == value and keys != ("mcp", bc_config.SERVER_NAME):
            leaked.append(".".join(keys))
    if leaked:
        report.add(WARN, f"live: your own config file {user_file.name} carries the pack's settings ({', '.join(leaked)}); "
                         "the pack keeps its own file, so these can go")
        report.filled[("pack-install", 2)] = f"FAIL (/doctor {date.today()}): {user_file.name} holds the pack's {', '.join(leaked)}"
    else:
        report.filled[("pack-install", 2)] = f"PASS (/doctor {date.today()}): {user_file.name} holds none of the pack's settings"


def live_run(report: Report, folder: Path, agent: desktop.DesktopAgent, scratch: Path, research: Path | None,
             version: str | None) -> None:
    """One tiny real request as a Desktop session: proves a session runs end to end and measures
    its per-step context (input plus cache read and write of the first step)."""
    result = agent.run_agent(scratch, None, PROMPT, model=MODEL)
    if result.stopped == "timeout":
        report.add(FAIL, "live: the tiny request didn't finish within 5 minutes; BharatCode may be down or queued")
        return
    first = result.steps[0] if result.steps else None
    tokens = (first.input_tokens + first.cache_read_tokens + first.cache_write_tokens) if first else 0
    reply = result.text.strip()
    if not tokens:
        report.add(FAIL, "live: the tiny request produced no step or reported no token usage: "
                         + (result.error or "no error given"))
        return
    with_pack = pack_installed(folder)
    state = "with" if with_pack else "without"
    report.add(PASS, f"live: a one-step request ran end to end on {waits.name(MODEL)} (reply {reply[:20]!r}); context sent "
                     f"before your words: {tokens:,} tokens, {state} the pack")
    if research is None:
        report.add(WARN, "live: this isn't a checkout, so the measurement isn't recorded (research/ is missing)")
        return
    runs = research / "pack-install" / "overhead-runs.jsonl"
    entry = {"date": date.today().isoformat(), "variant": "empty-mcp", "pack_installed": with_pack,
             "context_tokens": tokens, "model": MODEL, "desktop_version": version}
    with open(runs, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(entry) + "\n")
    latest: dict[bool, dict] = {}
    for n, line in enumerate(runs.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            report.add(WARN, f"live: {runs.name} line {n} isn't JSON and was skipped")
            continue
        # Only runs on the same model and Desktop version are comparable: tokenizers and system prompts differ.
        if isinstance(r, dict) and isinstance(r.get("context_tokens"), int) and r.get("model") == MODEL \
                and r.get("desktop_version") == version:
            latest[bool(r.get("pack_installed"))] = r
    if True in latest and False in latest:
        a, b = latest[False]["context_tokens"], latest[True]["context_tokens"]
        result = {"date": date.today().isoformat(), "variant": "empty-mcp", "model": MODEL, "desktop_version": version,
                  "without_pack": a, "with_pack": b, "added": b - a, "budget": BUDGET, "within_budget": b - a <= BUDGET,
                  "measured": {"without_pack": latest[False]["date"], "with_pack": latest[True]["date"]}}
        (research / "pack-install" / "overhead.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8",
                                                                  newline="\n")
        status = PASS if result["within_budget"] else FAIL
        report.add(status, f"live: the pack adds {b - a:,} tokens per step on {waits.name(MODEL)} ({a:,} without, measured "
                           f"{latest[False]['date']}; {b:,} with, measured {latest[True]['date']}; budget {BUDGET:,}); "
                           "written to research/pack-install/overhead.json")
        report.filled[("pack-install", 8)] = (f"{status} (/doctor live {date.today()}): added {b - a:,} tokens per step "
                                              f"on {waits.name(MODEL)}, budget {BUDGET:,}")
    else:
        other = "uninstalled (`gtu integrate --pack --uninstall`)" if with_pack else "installed (`gtu integrate --pack`)"
        report.add(WARN, f"live: measured {tokens:,} tokens {state} the pack; run /doctor live once more with the "
                         f"pack {other}, on the same BharatCode Desktop version, to measure the difference")
        report.filled[("pack-install", 8)] = (f"PARTIAL (/doctor live {date.today()}): {tokens:,} tokens per step "
                                              f"{state} the pack on {waits.name(MODEL)}; the other state is still to measure")


def live_copy(report: Report) -> None:
    """The job copy leaves build output out, on this machine's file system."""
    with tempfile.TemporaryDirectory(prefix="spare-gpu-doctor-") as scratch:
        root = Path(scratch)
        for rel in ("src/a.py", "dist/bundle.js", "node_modules/left-pad/index.js", "build/out.o"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x\n", encoding="utf-8")
        try:
            copied = sorted(worktree.project_files(root))
        except worktree.GitError as exc:
            report.add(FAIL, f"a job's copy can't be made here: {exc}")
            return
    if copied == ["src/a.py"]:
        report.add(PASS, "a job's copy leaves dist/, build/ and node_modules/ out")
        report.filled[("safety-fixes", 5)] = f"PASS (/doctor live {date.today()}): only src/a.py was copied from a folder with dist/, build/ and node_modules/"
    else:
        report.add(FAIL, f"a job's copy would include {', '.join(c for c in copied if c != 'src/a.py')}")
        report.filled[("safety-fixes", 5)] = f"FAIL (/doctor live {date.today()}): copied {', '.join(copied)}"


def live(report: Report, folder: Path, research: Path | None,
         make_agent: Callable[[], desktop.DesktopAgent] | None = None) -> None:
    live_copy(report)  # needs no Desktop: this machine's file system and git
    check_own_file(report, folder)
    try:
        agent = make_agent() if make_agent else desktop.DesktopAgent(desktop.discover(), timeout=300.0)
    except desktop.DesktopNotFound as exc:
        report.add(FAIL, f"live: the measurement runs as a BharatCode Desktop session, and Desktop wasn't found ({exc}); "
                         "run /doctor live from a Desktop chat. The checks above still hold")
        return
    with tempfile.TemporaryDirectory(prefix="spare-gpu-doctor-") as scratch:
        try:
            live_run(report, folder, agent, Path(scratch), research, agent.server_version())
        except (OSError, ValueError, KeyError) as exc:
            report.add(FAIL, f"live: the tiny request or its record failed: {type(exc).__name__}: {exc}")


# --- writing results into the manual-check tables ------------------------------------------------

def fill_manual_checks(research: Path, filled: dict[tuple[str, int], str]) -> list[str]:
    """Put each result into the Result column of its row (file, number). Returns what was written."""
    written = []
    by_file: dict[str, dict[int, str]] = {}
    for (name, row), text in sorted(filled.items()):
        by_file.setdefault(name, {})[row] = text
    for name, rows in by_file.items():
        path = research / name / "manual-check.md"
        if not path.is_file():
            continue
        with open(path, encoding="utf-8", newline="") as fh:  # keep the file's own line endings
            lines = fh.read().splitlines(keepends=True)
        for i, line in enumerate(lines):
            m = re.match(r"^\| (\d+) \|", line)
            if not m or int(m.group(1)) not in rows:
                continue
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            cells = body.split(" | ")
            cells[-1] = rows[int(m.group(1))].replace("|", "/") + " |"
            lines[i] = " | ".join(cells) + ending
            written.append(f"{name} {m.group(1)}")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
    return written


# --- the whole thing -----------------------------------------------------------------------------

def run(cwd: Path, channel: str = "stable", live_mode: bool = False, write: bool = True,
        research: Path | None = None, make_agent: Callable[[], desktop.DesktopAgent] | None = None) -> Report:
    report = Report()
    research = research or research_dir()
    check_tools(report)
    if channel == "stable":
        folder, how = bc_config.locate_config_dir()
    else:
        folder, how = bc_config.channel_config_dir(channel), f"BharatCode Desktop's {channel} channel"
    command = check_config(report, folder, how)
    if folder.is_dir():
        check_pack(report, folder, how, command)
        check_server(report, command)
    root = check_project(report, Path(cwd))
    check_settings(report, root)
    check_memory(report, root, folder)
    if live_mode:
        live(report, folder, research, make_agent)
        if write and research is not None and report.filled:
            written = fill_manual_checks(research, report.filled)
            if written:
                report.add(PASS, "manual-check rows filled from these results: " + ", ".join(written))
    return report


MANUAL_LEFT = ("pack-install 1, 3 to 7, 9 and 10, safety-fixes 1 to 4 and 6 to 12, and memory 1 to 7 need you at "
               "Desktop; each command's own research/<command>/manual-check.md too")
