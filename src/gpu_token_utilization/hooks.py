"""spare-gpu's git hooks: `gtu hooks install` writes them, `gtu guard` is what pre-commit runs.

pre-commit   runs `gtu guard`: the commit is stopped when the staged changes add a key-shaped string
             (redact.py's patterns) or add or change a file BharatCode Desktop or another assistant
             runs as code or reads as instructions: .opencode/tool, .opencode/plugin, .claude, .agents.
post-commit  queues a review of the commit just made (`gtu add review --commit HEAD`) and says so in
             one line. It only queues: jobs run in front of the user, with `gtu start`, never from a hook.

The hooks are POSIX sh scripts (git for Windows runs hooks with its own sh), call Python by its
absolute path so they work whatever PATH a terminal has, and carry a marker comment so only ours
are ever rewritten or removed. A hook the user wrote is left alone, with the line to add printed.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import redact, worktree

MARKER = "# spare-gpu hook"
HOOKS = ("pre-commit", "post-commit")
# Folders (project-relative, posix, lower case) a commit may not add to or change: Desktop runs
# .opencode/tool and .opencode/plugin as code, and .claude and .agents are read as instructions.
GUARDED = (".opencode/tool/", ".opencode/plugin/", ".claude/", ".agents/")
SKIP_ONCE = "git commit --no-verify"


class HookError(Exception):
    """The hooks couldn't be written, removed or run; the message says why and what to do."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None  # None: the whole file is the problem
    why: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"  {where}: {self.why}"


# --- the scripts -------------------------------------------------------------------------------

def sh_python(executable: str | None = None) -> str:
    """The Python running spare-gpu, as one quoted sh word. Windows paths get forward slashes
    (git's sh takes C:/Users/... as it is; backslashes would need escaping) and single quotes
    keep spaces, `$` and backticks literal."""
    path = executable or sys.executable
    if os.name == "nt":
        path = path.replace("\\", "/")
    return "'" + path.replace("'", "'\\''") + "'"


def one_liner(hook: str, python: str) -> str:
    """The one line a hook of the user's own needs to do what ours does."""
    if hook == "pre-commit":
        return f"{python} -m gpu_token_utilization guard || exit 1"
    return f"{python} -m gpu_token_utilization add review --commit HEAD --quiet"


def script(hook: str, python: str) -> str:
    """The hook as a POSIX sh script. The marker comment is how ours are recognised."""
    head = f"#!/bin/sh\n{MARKER}: {hook}, written by `gtu hooks install` (`gtu hooks install --uninstall` removes it).\n"
    if hook == "pre-commit":
        return head + (
            "# Stops a commit that adds a key-shaped string, or adds or changes a file under .opencode/tool,\n"
            f"# .opencode/plugin, .claude or .agents. To skip it once: {SKIP_ONCE}\n"
            f"PYTHON={python}\n"
            'if [ ! -x "$PYTHON" ]; then\n'
            '    echo "spare-gpu: $PYTHON is gone, so the pre-commit check was skipped. Run gtu hooks install again, or delete .git/hooks/pre-commit." >&2\n'
            "    exit 0\n"
            "fi\n"
            '"$PYTHON" -m gpu_token_utilization guard\n'
            "status=$?\n"
            'if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then\n'
            '    echo "spare-gpu: the pre-commit check could not run (exit $status). To remove it: gtu hooks install --uninstall" >&2\n'
            "fi\n"
            "exit $status\n"
        )
    return head + (
        "# Queues a spare-gpu review of the commit you just made. Nothing runs until you run: gtu start\n"
        f"PYTHON={python}\n"
        '[ -x "$PYTHON" ] || exit 0\n'
        '"$PYTHON" -m gpu_token_utilization add review --commit HEAD --quiet\n'
        "exit 0\n"
    )


# --- where they go -----------------------------------------------------------------------------

def hooks_dir(repo: Path) -> Path:
    """The hooks folder of the repo containing `repo` (.git/hooks, or the main repo's for a git
    worktree). Refuses a repo with core.hooksPath set: that folder is shared or managed by another
    tool (husky, say), so the user adds our line to its hooks instead."""
    repo = Path(repo)
    top = worktree._git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or not top.stdout.strip():
        raise HookError(f"{repo} is not in a git repo, so it has no hooks folder. Run this inside the repo.")
    custom = worktree._git(repo, "config", "--get", "core.hooksPath", check=False).stdout.strip()
    python = sh_python()
    if custom:
        raise HookError(
            f"this repo's hooks live in {custom} (core.hooksPath), a folder another tool manages, so nothing was "
            "written. To get the same checks, add one line to each hook there:\n"
            f"  pre-commit:   {one_liner('pre-commit', python)}\n"
            f"  post-commit:  {one_liner('post-commit', python)}"
        )
    out = worktree._git(repo, "rev-parse", "--git-path", "hooks").stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else Path(top.stdout.strip()) / path


def is_ours(path: Path) -> bool:
    """Whether a hook file carries spare-gpu's marker (read as bytes: it may be anything)."""
    try:
        with open(path, "rb") as f:
            return MARKER.encode() in f.read(4096)
    except OSError:
        return False


def _write(path: Path, text: str) -> None:
    """Write the hook whole or not at all: to a temporary file next to it, then one replace. LF
    line endings always (sh on Windows chokes on CRLF), so bytes, not text mode."""
    tmp = path.with_name(path.name + ".spare-gpu.tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HookError(f"couldn't write {path}: {exc}. If a program has it open, close it and run gtu hooks install again.") from None


def install(repo: Path, executable: str | None = None) -> list[str]:
    """Write our pre-commit and post-commit hooks into the repo's hooks folder. A hook that exists
    and isn't ours is left alone; the line to add to it is printed instead. Returns what happened,
    line by line."""
    folder = hooks_dir(repo)
    folder.mkdir(parents=True, exist_ok=True)
    python = sh_python(executable)
    lines = []
    for hook in HOOKS:
        path = folder / hook
        if path.exists() and not is_ours(path):
            lines.append(f"{path} is already there and isn't spare-gpu's, so it was left as it is. To run spare-gpu's "
                         f"{hook} step from it, add this line to it:\n  {one_liner(hook, python)}")
            continue
        existed = path.exists()
        _write(path, script(hook, python))
        lines.append(f"{'Updated' if existed else 'Wrote'} {path}.")
    lines.append("pre-commit stops a commit that adds a key-shaped string or changes .opencode/tool, .opencode/plugin, "
                 f".claude or .agents ({SKIP_ONCE} skips it once); post-commit queues a review of each commit, which "
                 "`gtu start` runs. Undo with: gtu hooks install --uninstall")
    return lines


def uninstall(repo: Path) -> list[str]:
    """Remove our hooks and only ours."""
    folder = hooks_dir(repo)
    lines = []
    for hook in HOOKS:
        path = folder / hook
        if not path.exists():
            continue
        if not is_ours(path):
            lines.append(f"{path} isn't spare-gpu's; left as it is.")
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise HookError(f"couldn't remove {path}: {exc}. If a program has it open, close it and try again.") from None
        lines.append(f"Removed {path}.")
    return lines or ["No spare-gpu hooks were installed here; nothing changed."]


# --- the pre-commit check --------------------------------------------------------------------

def _git_bytes(repo: Path, *args: str) -> bytes:
    """A git command's raw output: paths and lines come back as bytes, decoded here with
    replacement, since a staged file may be in any encoding."""
    try:
        proc = subprocess.run([*worktree.GIT, *args], cwd=repo, capture_output=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HookError("git isn't installed, so the staged changes can't be read.") from None
    if proc.returncode != 0:
        raise HookError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.stdout


def staged_files(repo: Path) -> list[tuple[str, str]]:
    """(status letter, path) for every staged change; a rename counts as a deletion and an addition,
    so a file moved into a guarded folder shows up as added there."""
    out = _git_bytes(repo, "diff", "--cached", "--name-status", "-z", "--no-renames")
    fields = [f.decode("utf-8", "replace") for f in out.split(b"\0") if f]
    return [(fields[i][:1], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def added_lines(repo: Path) -> list[tuple[str, int, str]]:
    """(path, line number in the file as it will be committed, text) for every added line in
    the staged changes."""
    out = _git_bytes(repo, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--unified=0",
                     "--no-renames")
    found, path, line = [], None, 0
    for raw in out.split(b"\n"):
        text = raw.decode("utf-8", "replace").rstrip("\r")
        if text.startswith("+++ "):
            name = text[4:].rstrip("\t")
            path = name[2:] if name.startswith("b/") else (None if name == "/dev/null" else name)
        elif text.startswith("@@ "):
            new = text.split(" +", 1)[1].split(" ", 1)[0]  # "+12,3" or "+12"
            line = int(new.split(",")[0])
        elif text.startswith("+") and path is not None:
            found.append((path, line, text[1:]))
            line += 1
        elif text.startswith(" "):
            line += 1
    return found


def guard(repo: Path) -> list[Finding]:
    """What in the staged changes would stop the commit: files added or changed under GUARDED, and
    added lines with a key-shaped string. Empty means the commit may go ahead."""
    findings = []
    for status, path in staged_files(repo):
        low = path.replace("\\", "/").lower()  # Windows is case-insensitive: .OpenCode is .opencode
        folder = next((g for g in GUARDED if low.startswith(g)), None)
        if folder and status in ("A", "M", "T"):  # added, changed, or type changed; a deletion is fine
            findings.append(Finding(path, None, f"files under {folder} are run or read by BharatCode Desktop. If you mean "
                                                f"to change it, commit it with: {SKIP_ONCE}"))
    for path, line, text in added_lines(repo):
        for match in redact.find(text):
            seen = text[match.start:match.start + 4]
            findings.append(Finding(path, line, f"adds what looks like a {match.label} ({seen}...). Move it out of the "
                                                "code (an environment variable, or a file git ignores) and commit again."))
    return findings


def explain(findings: list[Finding]) -> str:
    """The message a stopped commit prints."""
    return "\n".join([
        f"spare-gpu stopped this commit ({len(findings)} problem{'s' if len(findings) != 1 else ''}):",
        *(str(f) for f in findings),
        f"To commit anyway, skip the check once: {SKIP_ONCE}",
    ])
