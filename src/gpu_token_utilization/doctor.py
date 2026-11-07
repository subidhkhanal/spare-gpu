"""`gtu doctor`: one diagnosis of the whole install, each problem with its fix.

Without `--live` it starts nothing of BharatCode's and changes nothing: it reads spare-gpu's own
state, BharatCode's config folder, the project's .gtu folder, and starts spare-gpu's own MCP
server once, the way Desktop would, to see that it answers.

With `--live` it also asks BharatCode's CLI what Desktop really sees (`bharatcode debug config`,
`bharatcode debug agent build`), runs one tiny real request in an empty folder to measure the
per-step context (the pack's overhead budget), and writes the outcome into the manual-check
tables under research/ that these checks decide. The live part costs one short model call.
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

from . import __version__, agent_cli, bc_config, pack, router, runstate, waits, worktree
from .api import ApiKeyMissing, env_without_key, find_api_key
from .jobs import JobConfig
from .pack import check
from .pack.check import FAIL, PASS, WARN, Finding
from .store import FINAL_STATUSES, Store

PROMPT = "Reply with the single word OK."
BUDGET = 1_500  # tokens the pack may add to a normal session's step (plan §7)
SPARE_TOOLS = ("spare_delegate", "spare_waits", "spare_work_status", "spare_cancel_work", "spare_ask")
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


def check_cli(report: Report, status: agent_cli.CliStatus) -> None:
    if status.problem:
        report.add(WARN, "BharatCode's CLI isn't available. Desktop runs jobs without it; the CLI is needed for "
                         "`gtu doctor --live` and for the token figures of `bharatcode stats`. " + status.problem.splitlines()[0])
    elif status.signed_in is False:
        report.add(WARN, f"BharatCode's CLI {status.version} is installed but not signed in: run `bharatcode auth login`")
    else:
        report.add(PASS, f"BharatCode's CLI {status.version}" + (" is installed and signed in" if status.signed_in else
                                                                   " is installed (couldn't tell whether it's signed in)"))


def _registered_command(folder: Path) -> tuple[list[str] | None, Finding]:
    user = bc_config.config_file(folder)
    if not user.is_file():
        return None, Finding(FAIL, f"BharatCode's config file {user} is missing; run `gtu integrate`")
    try:
        doc = bc_config.parse_jsonc(user.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        return None, Finding(FAIL, f"BharatCode's config file {user} doesn't parse ({exc}); Desktop ignores a broken file. "
                                   f"Fix it by hand (a backup may be next to it as {user.name}{bc_config.BACKUP_SUFFIX})")
    entry = (doc.get("mcp") or {}).get(bc_config.SERVER_NAME) or {}
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
    else:
        report.add(PASS, "the /auto command is installed")
    return command


def pack_installed(folder: Path) -> bool:
    return any(bc_config.is_pack_file(folder / n) for n in bc_config.PACK_NAMES)


def check_pack(report: Report, folder: Path, how: str, command: list[str] | None) -> None:
    if not pack_installed(folder):
        report.add(PASS, "the command pack isn't installed (optional): `gtu integrate --pack` adds /deep-research, "
                         "/code-review, /security-review, /simplify and /verify-app")
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
    folder = root / ".gtu"
    db = folder / "gtu.db"
    if not db.is_file():
        report.add(PASS, "no jobs have run here yet (no .gtu folder)")
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
        report.add(WARN, f"a run file is left from process {pid}, which is gone (a crash or a killed terminal); "
                         "`gtu stop` tidies it and pauses any job left running")
    if runstate.stop_requested(folder) and not (pid and alive):
        report.add(WARN, "a stop request is pending with nothing running; the next job would stop at once. "
                         f"Delete {folder / runstate.STOP_FILE} or run `gtu stop`")
    running = [j.id for j in jobs if j.status == "running"] if not (pid and alive) else []
    if running:
        report.add(WARN, f"job(s) marked running with no runner: {', '.join(running)}; `gtu stop` pauses them so "
                         "`gtu start` can resume")
    paused = [j.id for j in jobs if j.status == "paused"]
    if paused:
        report.add(PASS, f"paused job(s) {', '.join(paused)}: `gtu start` resumes them")
    copies = folder / "worktrees"
    orphans = [p.name for p in copies.iterdir() if p.is_dir() and (p.name not in ids or ids[p.name].status in FINAL_STATUSES)] \
        if copies.is_dir() else []
    if orphans:
        report.add(WARN, f"leftover job copies with no unfinished job: {', '.join(orphans)} in {copies}; delete them "
                         "(nothing depends on them)")
    return root


def check_settings(report: Report, root: Path) -> None:
    try:
        config = JobConfig.from_env()
        report.add(PASS, f"test command for jobs: {' '.join(config.test_command)}")
    except ValueError as exc:
        report.add(FAIL, f"job settings: {exc}")
    try:
        found = find_api_key()
    except ApiKeyMissing as exc:
        found = None
        report.add(WARN, f"API key: {exc} (only needed to run jobs from a terminal outside Desktop)")
    if found:
        report.add(PASS, f"API key: found in {found[1]} (only needed outside Desktop)")
    elif not any(f.text.startswith("API key") for f in report.findings):
        report.add(PASS, "no API key set, which is fine inside Desktop; `gtu setkey` adds one for runs from a terminal")
    policy = router.load_policy(root / ".gtu")
    safe = sorted(k for k, v in (policy.get("types") or {}).items() if v == "switch-safe")
    report.add(PASS, "job types that may move to the faster model: " + (", ".join(safe) or "none")
                     + f" (routing_policy.json plus {root / '.gtu' / router.SWITCH_SAFE_FILE})")


# --- live checks ---------------------------------------------------------------------------------

def _json_from(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def _cli(command: list[str], args: list[str], cwd: Path, timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run([*command, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL, env=env_without_key(NO_COLOR="1"))


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


def live_config(report: Report, folder: Path, command: list[str], scratch: Path) -> None:
    proc = _cli(command, ["debug", "config"], scratch)
    merged = _json_from(proc.stdout)
    if merged is None:
        report.add(FAIL, "live: `bharatcode debug config` printed no JSON (Desktop may reject the config): "
                         + (proc.stderr.strip().splitlines() or ["no output"])[-1])
        return
    entry = (merged.get("mcp") or {}).get(bc_config.SERVER_NAME) or {}
    if not entry:
        report.add(FAIL, "live: Desktop's merged config has no spare-gpu MCP entry; run `gtu integrate`")
    else:
        report.add(PASS, "live: Desktop's merged config carries spare-gpu's MCP entry")
    report.filled[("pack-install", 1)] = f"PASS (gtu doctor --live {date.today()}): the CLI loaded the config with no error"
    if not pack_installed(folder):
        return
    helpers, tools, doc = expected_pack(folder)
    permission = merged.get("permission") or {}
    task = permission.get("task") if isinstance(permission.get("task"), dict) else {}
    loose_helpers = [h for h in helpers if task.get(h) != "deny"]
    loose_tools = [t for t in tools if permission.get(t) != "deny"]
    if loose_helpers or loose_tools:
        report.add(FAIL, "live: in Desktop's merged config these are not denied for other agents: "
                         + ", ".join(loose_helpers + loose_tools) + "; run `gtu integrate --pack` and check "
                         "opencode.jsonc doesn't override permission")
        report.filled[("pack-install", 6)] = f"FAIL (gtu doctor --live {date.today()}): not denied: {', '.join(loose_helpers + loose_tools)}"
    else:
        report.add(PASS, f"live: Desktop's merged config denies the pack's {len(helpers)} helpers and {len(tools)} tools to every other agent")
        report.filled[("pack-install", 6)] = (f"PASS (gtu doctor --live {date.today()}): permission.task denies all "
                                              f"{len(helpers)} helpers in the merged config Desktop uses for the task tool's list")
    if doc.get("small_model") and merged.get("small_model") != doc.get("small_model"):
        report.add(WARN, f"live: small_model is {merged.get('small_model')} in the merged config, not the pack's "
                         f"{doc.get('small_model')}; something later in the load order overrides it")
    user_doc = {}
    try:
        user_doc = bc_config.parse_jsonc(bc_config.config_file(folder).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    leaked = [k for k in ("small_model", "agent") if k in user_doc] + \
             [f"permission.{k}" for k in (user_doc.get("permission") or {}) if k == "task" or k.startswith(f"{pack.SERVER}_")]
    if leaked:
        report.add(WARN, f"live: your own config file carries pack settings ({', '.join(leaked)}); the pack keeps its own file")
        report.filled[("pack-install", 2)] = f"FAIL (gtu doctor {date.today()}): opencode.jsonc holds {', '.join(leaked)}"
    else:
        report.filled[("pack-install", 2)] = f"PASS (gtu doctor {date.today()}): your own file holds none of the pack's keys"


def live_agent(report: Report, folder: Path, command: list[str], scratch: Path) -> None:
    proc = _cli(command, ["debug", "agent", "build"], scratch)
    agent = _json_from(proc.stdout)
    if agent is None or not isinstance(agent.get("tools"), dict):
        report.add(FAIL, "live: `bharatcode debug agent build` printed no tool list: "
                         + (proc.stderr.strip().splitlines() or ["no output"])[-1])
        return
    tools = agent["tools"]
    spare = [f"{pack.SERVER}_{t}" for t in SPARE_TOOLS]
    seen = [t for t in spare if tools.get(t)]
    if len(seen) == len(spare):
        report.add(PASS, "live: a plain chat sees spare-gpu's five tools")
    else:
        report.add(WARN, f"live: a plain chat sees {len(seen)} of spare-gpu's five tools; the MCP server may not have "
                         "connected (`bharatcode mcp list` shows why)")
    if not pack_installed(folder):
        return
    _, pack_tools, _ = expected_pack(folder)
    visible = [t for t in pack_tools if tools.get(t)]
    if visible:
        report.add(FAIL, f"live: a plain chat can see the pack-only tools {', '.join(visible)}; they cost tokens on every "
                         "step. Run `gtu integrate --pack`")
        report.filled[("pack-install", 7)] = f"FAIL (gtu doctor --live {date.today()}): visible: {', '.join(visible)}"
    else:
        report.add(PASS, f"live: none of the pack's {len(pack_tools)} tools is in a plain chat's tool list")
        report.filled[("pack-install", 7)] = (f"PASS (gtu doctor --live {date.today()}): `debug agent build` lists none of "
                                              f"{', '.join(pack_tools)}; {len(seen)} of the five /auto tools present")
    leads = [n for c in pack.select(",".join(check.installed_components(folder)) or None) for n in c.agents if n.endswith("-lead")] \
        if check.installed_components(folder) else []
    if leads:
        lead = _json_from(_cli(command, ["debug", "agent", leads[0]], scratch).stdout) or {}
        own = [t for t in pack_tools if (lead.get("tools") or {}).get(t)]
        report.add(PASS if own else WARN, f"live: the {leads[0]} agent sees {len(own)} of the pack's tools"
                                          + ("" if own else " (none: is spare-gpu's server started with --pack?)"))


def context_tokens(stdout: str) -> tuple[int | None, str]:
    """The first step's context (input plus cache) from `bharatcode run --format json`, and the reply."""
    first, reply = None, ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        if event.get("type") == "step_finish" and first is None:
            t = part.get("tokens") or {}
            first = (t.get("input") or 0) + ((t.get("cache") or {}).get("read") or 0) + ((t.get("cache") or {}).get("write") or 0)
        elif event.get("type") == "text":
            reply += part.get("text") or ""
    return first, reply.strip()


def live_run(report: Report, folder: Path, command: list[str], scratch: Path, research: Path | None,
             version: str | None) -> None:
    """One tiny real request: proves a session runs end to end and measures its per-step context."""
    try:
        proc = _cli(command, ["run", "--dir", str(scratch), "--format", "json", PROMPT], scratch)
    except subprocess.TimeoutExpired:
        report.add(FAIL, "live: the tiny request didn't finish within 5 minutes; BharatCode may be down or queued")
        return
    tokens, reply = context_tokens(proc.stdout)
    if tokens is None:
        report.add(FAIL, "live: the tiny request produced no step (exit code {}): {}".format(
            proc.returncode, (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or ["no output"])[-1]))
        return
    with_pack = pack_installed(folder)
    report.add(PASS, f"live: a one-step request ran end to end (reply {reply[:20]!r}); context sent before your words: "
                     f"{tokens:,} tokens, {'with' if with_pack else 'without'} the pack")
    if research is None:
        report.add(WARN, "live: this isn't a checkout, so the measurement isn't recorded (research/ is missing)")
        return
    runs = research / "pack-install" / "overhead-runs.jsonl"
    entry = {"date": date.today().isoformat(), "variant": "empty-mcp", "pack_installed": with_pack,
             "context_tokens": tokens, "cli_version": version}
    with open(runs, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    recorded = [json.loads(l) for l in runs.read_text(encoding="utf-8").splitlines() if l.strip()]
    latest = {}
    for r in recorded:
        latest[bool(r.get("pack_installed"))] = r
    if True in latest and False in latest:
        a, b = latest[False]["context_tokens"], latest[True]["context_tokens"]
        result = {"date": date.today().isoformat(), "variant": "empty-mcp", "without_pack": a, "with_pack": b,
                  "added": b - a, "budget": BUDGET, "within_budget": b - a <= BUDGET}
        (research / "pack-install" / "overhead.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        status = PASS if result["within_budget"] else FAIL
        report.add(status, f"live: the pack adds {b - a:,} tokens per step ({a:,} without, {b:,} with; budget {BUDGET:,}); "
                           "written to research/pack-install/overhead.json")
        report.filled[("pack-install", 8)] = f"{status} (gtu doctor --live {date.today()}): added {b - a:,} tokens per step, budget {BUDGET:,}"
    else:
        other = "uninstalled (`gtu integrate --pack --uninstall`)" if with_pack else "installed (`gtu integrate --pack`)"
        report.add(WARN, f"live: measured {tokens:,} tokens {'with' if with_pack else 'without'} the pack; run "
                         f"`gtu doctor --live` once more with the pack {other} to measure the difference")
        report.filled[("pack-install", 8)] = (f"PARTIAL (gtu doctor --live {date.today()}): {tokens:,} tokens per step "
                                              f"{'with' if with_pack else 'without'} the pack; the other state is still to measure")


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
        report.filled[("safety-fixes", 5)] = f"PASS (gtu doctor --live {date.today()}): only src/a.py was copied from a folder with dist/, build/ and node_modules/"
    else:
        report.add(FAIL, f"a job's copy would include {', '.join(c for c in copied if c != 'src/a.py')}")
        report.filled[("safety-fixes", 5)] = f"FAIL (gtu doctor --live {date.today()}): copied {', '.join(copied)}"


def live(report: Report, folder: Path, status: agent_cli.CliStatus, research: Path | None) -> None:
    live_copy(report)  # needs no CLI: this machine's file system and git
    if status.problem or not status.command:
        report.add(FAIL, "live checks need BharatCode's CLI, which isn't available; the static checks above still hold")
        return
    command = status.command
    with tempfile.TemporaryDirectory(prefix="spare-gpu-doctor-") as scratch:
        root = Path(scratch)
        for step in (live_config, live_agent):
            try:
                step(report, folder, command, root)
            except (OSError, subprocess.TimeoutExpired) as exc:
                report.add(FAIL, f"live: {step.__name__.replace('live_', 'bharatcode debug ')} failed: {exc}")
        try:
            live_run(report, folder, command, root, research, status.version)
        except OSError as exc:
            report.add(FAIL, f"live: the tiny request couldn't start: {exc}")


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
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            m = re.match(r"^\| (\d+) \|", line)
            if not m or int(m.group(1)) not in rows:
                continue
            cells = line.rstrip("\n").split(" | ")
            cells[-1] = rows[int(m.group(1))].replace("|", "/") + " |"
            lines[i] = " | ".join(cells) + "\n"
            written.append(f"{name} {m.group(1)}")
        path.write_text("".join(lines), encoding="utf-8")
    return written


# --- the whole thing -----------------------------------------------------------------------------

def run(cwd: Path, channel: str = "stable", live_mode: bool = False, write: bool = True,
        cli_status: Callable[[], agent_cli.CliStatus] = agent_cli.cli_status,
        research: Path | None = None) -> Report:
    report = Report()
    research = research or research_dir()
    check_tools(report)
    status = cli_status()
    check_cli(report, status)
    if channel == "stable":
        folder, how = bc_config.locate_config_dir()
    else:
        folder, how = bc_config.channel_config_dir(channel, ask_cli=False), f"BharatCode Desktop's {channel} channel"
    command = check_config(report, folder, how)
    if folder.is_dir():
        check_pack(report, folder, how, command)
        check_server(report, command)
    root = check_project(report, Path(cwd))
    check_settings(report, root)
    if live_mode:
        live(report, folder, status, research)
        if write and research is not None and report.filled:
            written = fill_manual_checks(research, report.filled)
            if written:
                report.add(PASS, "manual-check rows filled from these results: " + ", ".join(written))
    return report


MANUAL_LEFT = ("pack-install 3, 4, 5, 9 and 10, and safety-fixes 1 to 4 and 6 to 12 need you at Desktop; "
               "each command's own research/<command>/manual-check.md too")
