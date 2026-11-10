"""The job type and the steps it is made of.

fix:    write   - a Desktop session applies the review findings in the job's note; saved as a patch.
        verify  - changes to existing test files are reverted, the test command runs, and the fix
                  is kept only if it passes.

Saving the session's work as a patch before checking it means a crash during the check never loses
it and never leaves unchecked work committed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import worktree
from .agent import CONFIG_FILE, AgentResult, BharatCodeError, BusyError
from .api import env_without_key
from .budget import BudgetExceeded
from .desktop import DESKTOP_NEEDED, DesktopNotFound
from .runstate import StopRequested

if TYPE_CHECKING:
    from .scheduler import StepContext

DEFAULT_ALLOWED = ("python -m pytest", "pytest", "npm test", "go test")
def _test_env() -> dict:
    """Model-written code never sees the API key, and leaves no bytecode caches behind."""
    return env_without_key(PYTHONDONTWRITEBYTECODE="1")


@dataclass
class JobConfig:
    test_command: tuple[str, ...] = ("python", "-m", "pytest")
    allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED
    test_timeout: float = 300.0
    agent_timeout: float = 900.0  # one job step, as one Desktop session
    @classmethod
    def load(cls, root: Path | None) -> JobConfig:
        """Defaults, then the project's .gtu/settings.json (`root` None: no file), then the
        GTU_* environment variables, which win. Raises ValueError (settings.SettingsError for the
        file) when the result can't be used."""
        from . import settings

        cfg = cls()
        found = settings.load(root)
        if found.test_command:
            cfg.test_command = found.test_command
        elif root is not None:  # no test command set: the one the project's files point to
            cfg.test_command = tuple(shlex.split(settings.detect_test_command(root)[0]))
            prefix = cfg.test_command[:2]
            if found.get("allowed_commands") is None and \
                    not any(tuple(a.split()[:2]) == prefix for a in cfg.allowed_commands):
                cfg.allowed_commands = (*cfg.allowed_commands, " ".join(prefix))
        if found.get("allowed_commands") is not None:
            cfg.allowed_commands = tuple(found.get("allowed_commands"))
        if found.get("test_timeout") is not None:
            cfg.test_timeout = float(found.get("test_timeout"))
        if found.get("agent_timeout") is not None:
            cfg.agent_timeout = float(found.get("agent_timeout"))
        return cls._from_env(cfg)

    @classmethod
    def from_env(cls) -> JobConfig:
        """The environment's settings alone (no settings file)."""
        return cls.load(None)

    @classmethod
    def _from_env(cls, cfg: JobConfig) -> JobConfig:
        if os.environ.get("GTU_TEST_COMMAND"):
            cfg.test_command = tuple(shlex.split(os.environ["GTU_TEST_COMMAND"]))
        if os.environ.get("GTU_ALLOWED_COMMANDS"):
            cfg.allowed_commands = tuple(c.strip() for c in os.environ["GTU_ALLOWED_COMMANDS"].split(",") if c.strip())
        if os.environ.get("GTU_TEST_TIMEOUT"):
            cfg.test_timeout = float(os.environ["GTU_TEST_TIMEOUT"])
        if os.environ.get("GTU_AGENT_TIMEOUT"):
            cfg.agent_timeout = float(os.environ["GTU_AGENT_TIMEOUT"])
        cfg.check()
        return cfg

    def check(self) -> None:
        if not command_allowed(self.test_command, self.allowed_commands):
            raise ValueError(f"test command {' '.join(self.test_command)!r} is not in the allowlist {self.allowed_commands}")


PYTHON_LAUNCHER = re.compile(r"^(python|pythonw|py)(\d+(\.\d+)?)?$")


def _program(word: str) -> str:
    """The program's name for the allowlist: any folder and .exe dropped, and the Python
    launchers (py, python3, python3.12, pythonw) all counted as "python"."""
    name = re.split(r"[\\/]", word)[-1]  # either separator: the config may name a Windows path on any platform
    stem = Path(name).stem.lower()
    return "python" if PYTHON_LAUNCHER.match(stem) else stem


def command_allowed(command: tuple[str, ...], allowed: tuple[str, ...]) -> bool:
    words = [_program(command[0]), *command[1:]] if command else []
    return any(words[: len(a.split())] == [_program(a.split()[0]), *a.split()[1:]] for a in allowed if a.split())


# --- git helpers on the job's worktree -------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run([*worktree.GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise BharatCodeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def changed_files(workdir: Path) -> list[tuple[str, str]]:
    """(git status code, path) for every change in the worktree, except the job's permission file and caches.
    -z: names come back raw (spaces, quotes, non-ASCII) instead of quoted and escaped."""
    out = []
    entries = _git(workdir, "status", "--porcelain", "-z", "-uall").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # a rename or copy: the next entry is the old name
        if path != CONFIG_FILE and not _generated(path):
            out.append((code, path))
    return out


def _generated(path: str) -> bool:
    parts = path.split("/")
    return "__pycache__" in parts or ".pytest_cache" in parts or path.endswith(".pyc")


def save_candidate(workdir: Path, patch: Path) -> list[str]:
    """Move everything changed in the worktree into a patch file, leaving the worktree clean."""
    paths = [p for _, p in changed_files(workdir)]
    patch.parent.mkdir(parents=True, exist_ok=True)
    if not paths:
        patch.write_bytes(b"")
        return []
    _git(workdir, "add", "-A", "--", ".", f":(exclude){CONFIG_FILE}",
         *(f":(exclude,glob){g}" for g in worktree.GENERATED))
    # Raw bytes: a text-mode write would turn line endings into CRLF on Windows and break the patch.
    diff = subprocess.run([*worktree.GIT, "diff", "--cached", "--binary", "HEAD"], cwd=workdir,
                          capture_output=True, check=True)
    patch.write_bytes(diff.stdout)
    _git(workdir, "reset", "--quiet", "--hard", "HEAD")
    _git(workdir, "clean", "-fdq")
    return paths


def apply_candidate(workdir: Path, patch: Path) -> bool:
    if not patch.exists() or not patch.read_bytes().strip():
        return False
    _git(workdir, "apply", "--whitespace=nowarn", str(patch))
    return True


def revert(workdir: Path, code: str, path: str) -> None:
    if code.strip() == "??" or code.strip() == "A":
        (workdir / path).unlink(missing_ok=True)
    else:
        _git(workdir, "checkout", "HEAD", "--", path)



# --- the agent step -----------------------------------------------------------------------

def run_agent(ctx: StepContext, prompt: str, *, edit: bool, commands: tuple[str, ...]) -> AgentResult:
    if ctx.agent is None:
        raise DesktopNotFound(DESKTOP_NEEDED)
    budget = ctx.budget_remaining()
    if budget <= 0:
        raise BudgetExceeded("token budget used up before the session could start")
    result = ctx.agent.run(
        ctx.workdir, prompt, edit=edit, commands=commands, token_budget=budget, on_step=ctx.log_agent_step,
        should_stop=ctx.should_stop,
    )
    if result.stopped == "stop":
        raise StopRequested("stopped")
    label = getattr(ctx.agent, "label", "BharatCode Desktop")
    if result.stopped == "budget":
        raise BudgetExceeded(f"stopped {label} after {result.tokens} tokens: budget reached")
    if result.stopped == "timeout":
        raise BusyError(f"{label} did not finish within {ctx.agent.timeout:.0f} s")
    if not result.ok:
        raise BharatCodeError(f"{label} failed: {result.error or 'unknown error'}")
    return result


def _agent_summary(result: AgentResult, saved: list[str]) -> str:
    return (
        f"BharatCode Desktop: {len(result.steps)} step(s), {result.tokens} tokens; "
        f"changed {len(saved)} file(s): {', '.join(saved) or 'none'}.\n{result.text}"
    ).strip()


TEST_FILE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|[^/]*\.test\.[jt]sx?|[^/]*_test\.go)$")


# --- the project's test command, run whole --------------------------------------------------

@dataclass
class TestRun:
    ok: bool
    why: str  # "exit code 1", or "it timed out after 300 s"
    output: str  # what the command printed, stdout then stderr, for the model and the user


def _as_text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def run_test_command(ctx: StepContext) -> TestRun:
    """Run the project's test command once in the job's copy, with the test environment (no API
    key, no bytecode caches). The command must be in the allowlist."""
    if not command_allowed(ctx.config.test_command, ctx.config.allowed_commands):
        raise BharatCodeError("the test command is not in the allowlist")
    try:
        proc = subprocess.run(list(ctx.config.test_command), cwd=ctx.workdir, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=ctx.config.test_timeout,
                              stdin=subprocess.DEVNULL, env=_test_env())
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in (_as_text(exc.stdout), _as_text(exc.stderr)) if part.strip())
        return TestRun(False, f"it timed out after {ctx.config.test_timeout:.0f} s", output.strip())
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part and part.strip())
    return TestRun(proc.returncode == 0, f"exit code {proc.returncode}", output.strip())


# --- fix (the findings of /code-review --fix) ----------------------------------------------

FIX_PROMPT = """Apply the review findings below to the code in this repository.
- Change only what each finding needs; keep every change small and local.
- Do not modify or delete existing test files; you may add new ones.
- Do not change behaviour beyond what a finding describes.
- You may run `{test_command}` to check your work. Do not run any other commands.
When you are finished, reply with one line per finding: fixed, or why not.

Findings:
{findings}"""


def write_fix(ctx: StepContext) -> str:
    job = ctx.job
    if not (job.note or "").strip():
        raise BharatCodeError("a fix job needs the findings to apply, in its note")
    prompt = FIX_PROMPT.format(findings=job.note.strip(), test_command=" ".join(ctx.config.test_command))
    result = run_agent(ctx, prompt, edit=True, commands=ctx.config.allowed_commands)
    saved = save_candidate(ctx.workdir, ctx.state_dir / "fix.patch")
    return _agent_summary(result, saved)


def verify_fix(ctx: StepContext) -> str:
    """Keep the fix only if the project's test command passes with it. Changes to existing test
    files are reverted first: a fix may not pass by weakening the tests."""
    if not apply_candidate(ctx.workdir, ctx.state_dir / "fix.patch"):
        return "No fix was generated; nothing to keep."
    if not command_allowed(ctx.config.test_command, ctx.config.allowed_commands):
        raise BharatCodeError("the test command is not in the allowlist")
    notes = []
    for code, path in changed_files(ctx.workdir):
        if TEST_FILE.search(path) and code.strip() != "??":
            revert(ctx.workdir, code, path)
            notes.append(f"reverted {path}: a fix may not change existing tests")
    changed = [p for _, p in changed_files(ctx.workdir)]
    if not changed:
        return "Kept the fix in 0 file(s): nothing was left to keep.\n" + "\n".join(f"- {n}" for n in notes)
    run = run_test_command(ctx)
    if not run.ok:
        for code, path in changed_files(ctx.workdir):
            revert(ctx.workdir, code, path)
        notes.append(f"discarded the fix: the test command failed with it ({run.why})")
        return "Kept the fix in 0 file(s): the test command failed with it.\n" + "\n".join(f"- {n}" for n in notes)
    notes = [f"kept {p}" for p in changed] + notes
    return f"Kept the fix in {len(changed)} file(s): the test command passed.\n" + "\n".join(f"- {n}" for n in notes)


PLANS = {
    "fix": [("write", write_fix), ("verify", verify_fix)],
}
