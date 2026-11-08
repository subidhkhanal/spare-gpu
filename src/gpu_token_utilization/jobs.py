"""The job types and the steps they are made of.

tests:  write   - a BharatCode Desktop session writes new test files (it may run allowlisted test commands);
                  the result is saved as a patch outside the worktree, nothing is committed yet.
        verify  - we apply the patch, run each new test file ourselves with the allowlisted
                  test command, drop failing tests, discard files that still fail, revert any
                  change that is not a new test file, and commit what is left.
docs:   write   - the session adds docstrings and README text (no commands allowed); saved as a patch.
        verify  - we keep a Python file only if its code is unchanged apart from docstrings and no
                  existing docstring was removed or shortened, and a Markdown file only if no line
                  was removed; everything else is reverted. Then commit.
review: review  - our own client sends the code (with line numbers) and writes the answer to
                  gtu-review-<job-id>.md. No source file is changed.
fix:    write   - a Desktop session applies the review findings in the job's note; saved as a patch.
        verify  - changes to existing test files are reverted, the test command runs, and the fix
                  is kept only if it passes.
green:  write   - the test command runs in the copy first; if it fails, a Desktop session gets its output
                  and fixes the code (never the tests); saved as a patch. If it passes, nothing to do.
        verify  - changes to existing tests reverted, the test command run again. Passing: done.
                  Failing: another write and verify step are added, up to green_rounds rounds in
                  all; after the last, the copy goes back to its starting files, so nothing is kept.
implement: write - a Desktop session carries out a Markdown plan file (the target); saved as a patch.
        verify  - like fix: kept only if the test command passes.

Saving the session's work as a patch before checking it means a crash during the check never loses
it and never leaves unchecked work committed.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import worktree
from .agent import CONFIG_FILE, AgentResult
from .api import env_without_key
from .budget import BudgetExceeded
from .client import BharatCodeError, BusyError
from .desktop import DESKTOP_NEEDED, DesktopNotFound
from .runstate import StopRequested

if TYPE_CHECKING:
    from .scheduler import StepContext

DEFAULT_ALLOWED = ("python -m pytest", "pytest", "npm test", "go test")
def _test_env() -> dict:
    """Model-written tests never see the API key, and leave no bytecode caches behind."""
    return env_without_key(PYTHONDONTWRITEBYTECODE="1")
SKIP_DIRS = worktree.SKIP_DIRS  # the review listing skips what the job's copy skips
MAX_FILE_BYTES = 200_000
MAX_GREEN_ROUNDS = 10  # the most write-and-check rounds a green job may be given (settings: green_rounds)
GREEN_ROUNDS_ENV = "GTU_GREEN_ROUNDS"


@dataclass
class JobConfig:
    test_command: tuple[str, ...] = ("python", "-m", "pytest")
    allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED
    test_timeout: float = 300.0
    agent_timeout: float = 900.0  # one job step, as one Desktop session
    review_max_chars: int = 40_000
    review_max_tokens: int = 4_000
    green_rounds: int = 3  # write-and-check rounds a green job gets to make the tests pass

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
        if found.get("allowed_commands") is not None:
            cfg.allowed_commands = tuple(found.get("allowed_commands"))
        if found.get("test_timeout") is not None:
            cfg.test_timeout = float(found.get("test_timeout"))
        if found.get("agent_timeout") is not None:
            cfg.agent_timeout = float(found.get("agent_timeout"))
        if found.get("green_rounds") is not None:
            cfg.green_rounds = int(found.get("green_rounds"))
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
        if os.environ.get(GREEN_ROUNDS_ENV):
            try:
                cfg.green_rounds = int(os.environ[GREEN_ROUNDS_ENV])
            except ValueError:
                raise ValueError(f"{GREEN_ROUNDS_ENV} must be a whole number from 1 to {MAX_GREEN_ROUNDS}, "
                                 f"not {os.environ[GREEN_ROUNDS_ENV]!r}") from None
        cfg.check()
        return cfg

    def check(self) -> None:
        if not command_allowed(self.test_command, self.allowed_commands):
            raise ValueError(f"test command {' '.join(self.test_command)!r} is not in the allowlist {self.allowed_commands}")
        rounds = self.green_rounds
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_GREEN_ROUNDS:
            raise ValueError(f"green_rounds must be a whole number from 1 to {MAX_GREEN_ROUNDS}, not {rounds!r}")


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


# --- text as Windows files actually are: byte-order marks and CRLF -------------------------

BOM = b"\xef\xbb\xbf"


def source_text(data: bytes) -> str:
    """Text for parsing and comparing: no byte-order mark (Python's parser rejects one in a
    str), and \\n line endings, so CRLF vs LF never counts as a change."""
    return data.removeprefix(BOM).decode("utf-8", "replace").replace("\r\n", "\n")


def match_file_style(file: Path, original: bytes) -> None:
    """Give a rewritten file the original's byte-order mark and line endings. The model's edit
    tools (and many editors) normalise both, which would make the branch's diff show every line changed."""
    data = file.read_bytes()
    body = data.removeprefix(BOM).replace(b"\r\n", b"\n")
    if original.count(b"\r\n") * 2 > original.count(b"\n"):  # mostly CRLF
        body = body.replace(b"\n", b"\r\n")
    if original.startswith(BOM):
        body = BOM + body
    if body != data:
        file.write_bytes(body)


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


def head_bytes(workdir: Path, path: str) -> bytes | None:
    """The file as committed on the job's branch, exactly (raw bytes, no decoding)."""
    proc = subprocess.run([*worktree.GIT, "show", f"HEAD:{path}"], cwd=workdir, capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def head_text(workdir: Path, path: str) -> str | None:
    data = head_bytes(workdir, path)
    return None if data is None else source_text(data)


# --- the agent step shared by tests and docs ---------------------------------------------

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
        raise StopRequested("stopped by `gtu stop`")
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


# --- tests -------------------------------------------------------------------------------

TESTS_PROMPT = """Write tests for the code in `{target}` in this repository.
- Create new test files only, named tests/test_gtu_<module>.py (create the tests folder if needed).
- Do not modify or delete any existing file.
- Cover real behaviour, including edge cases and error cases.{note}
- You may run `{test_command}` to check your tests. Do not run any other commands.
When you are finished, reply with one line listing the files you created."""

TEST_FILE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|[^/]*\.test\.[jt]sx?|[^/]*_test\.go)$")


def write_tests(ctx: StepContext) -> str:
    job = ctx.job
    prompt = TESTS_PROMPT.format(
        target=job.target, test_command=" ".join(ctx.config.test_command),
        note=f"\n- The developer adds: {job.note}" if job.note else "",
    )
    result = run_agent(ctx, prompt, edit=True, commands=ctx.config.allowed_commands)
    saved = save_candidate(ctx.workdir, ctx.state_dir / "tests.patch")
    return _agent_summary(result, saved)


def _test_names(xml_path: Path) -> tuple[set[str], set[str]]:
    """(passed, failed) test function names from a junit xml report; parameters are stripped."""
    passed, failed = set(), set()
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        name = re.sub(r"\[.*\]$", "", case.get("name", ""))
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(name)
        elif case.find("skipped") is None:
            passed.add(name)
    return passed - failed, failed  # a parametrised test counts as failing if any case fails


def _run_pytest(ctx: StepContext, path: str) -> tuple[str, set[str], set[str]]:
    """('ok' | 'failed' | 'broken', passed, failed) for one test file."""
    xml = ctx.state_dir / "junit.xml"
    xml.unlink(missing_ok=True)
    command = [*ctx.config.test_command, path, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
    try:
        proc = subprocess.run(command, cwd=ctx.workdir, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ctx.config.test_timeout, stdin=subprocess.DEVNULL,
                              env=_test_env())
    except subprocess.TimeoutExpired:
        return "broken", set(), set()
    if proc.returncode not in (0, 1) or not xml.exists():
        return "broken", set(), set()  # collection error, usage error, interrupted or no tests
    passed, failed = _test_names(xml)
    if not passed and not failed:
        return "broken", set(), set()
    return ("ok" if not failed else "failed"), passed, failed


def remove_test_functions(file: Path, names: set[str]) -> int:
    """Delete the named test functions (and their decorators) from a Python file."""
    original = file.read_bytes()
    source = source_text(original)
    tree = ast.parse(source)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            spans.append((start, node.end_lineno))
    kept: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if kept and start <= kept[-1][1]:
            continue  # nested inside a function that goes anyway: deleting it twice would eat the lines after
        kept.append((start, end))
    lines = source.splitlines(keepends=True)
    for start, end in reversed(kept):
        del lines[start - 1 : end]
    file.write_bytes("".join(lines).encode("utf-8"))
    match_file_style(file, original)  # same byte-order mark and line endings as the model wrote
    return len(kept)


def _keep_passing_python(ctx: StepContext, path: str) -> tuple[int, int, str]:
    """(tests kept, tests dropped, note). Deletes the file if nothing in it can be kept."""
    file = ctx.workdir / path
    status, passed, failed = _run_pytest(ctx, path)
    dropped = 0
    if status == "failed":
        try:
            dropped = remove_test_functions(file, failed)
        except SyntaxError:
            status = "broken"
        else:
            status, passed, failed = _run_pytest(ctx, path)
    if status != "ok":
        file.unlink(missing_ok=True)
        return 0, len(passed) + len(failed) + dropped, f"discarded {path}: its tests did not pass or could not run"
    return len(passed), dropped, f"kept {path}: {len(passed)} passing test(s)" + (f", dropped {dropped} failing" if dropped else "")


def _keep_passing_other(ctx: StepContext, path: str) -> tuple[int, int, str]:
    """Non-Python test files: run the whole test command once; keep the file only if it passes."""
    try:
        proc = subprocess.run(list(ctx.config.test_command), cwd=ctx.workdir, capture_output=True,
                              timeout=ctx.config.test_timeout, stdin=subprocess.DEVNULL, env=_test_env())
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    if not ok:
        (ctx.workdir / path).unlink(missing_ok=True)
        return 0, 1, f"discarded {path}: the test command failed with it"
    return 1, 0, f"kept {path}: the test command passed"


def verify_tests(ctx: StepContext) -> str:
    if not apply_candidate(ctx.workdir, ctx.state_dir / "tests.patch"):
        return "No test files were generated; nothing to keep."
    if not command_allowed(ctx.config.test_command, ctx.config.allowed_commands):
        raise BharatCodeError("the test command is not in the allowlist")
    notes, kept, dropped = [], 0, 0
    for code, path in changed_files(ctx.workdir):
        if code.strip() != "??" or not TEST_FILE.search(path):
            revert(ctx.workdir, code, path)
            notes.append(f"reverted {path}: tests jobs only add new test files")
            continue
        if path.endswith(".py") and "pytest" in " ".join(ctx.config.test_command):
            k, d, note = _keep_passing_python(ctx, path)
        else:
            k, d, note = _keep_passing_other(ctx, path)
        kept, dropped = kept + k, dropped + d
        notes.append(note)
    return f"Kept {kept} passing test(s), dropped {dropped}.\n" + "\n".join(f"- {n}" for n in notes)


# --- docs ----------------------------------------------------------------------------------

DOCS_PROMPT = """Improve the documentation of `{target}` in this repository.
- Add a docstring to every function, method and class that lacks one, and expand unclear ones.
- If README.md has no section about this code, add a short one at the end.
- Never delete, shorten or rewrite existing documentation, comments or README text; only add.
- Do not change any code: no renames, no logic or formatting changes.{note}
You cannot run commands. When you are finished, reply with one line listing the files you changed."""

DOC_FILES = (".md", ".rst", ".txt")


def write_docs(ctx: StepContext) -> str:
    job = ctx.job
    prompt = DOCS_PROMPT.format(target=job.target, note=f"\n- The developer adds: {job.note}" if job.note else "")
    result = run_agent(ctx, prompt, edit=True, commands=())
    saved = save_candidate(ctx.workdir, ctx.state_dir / "docs.patch")
    return _agent_summary(result, saved)


class _StripDocstrings(ast.NodeTransformer):
    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _strip


def _docstrings(tree: ast.AST) -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(node, prefix):
        doc = ast.get_docstring(node, clean=True) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if doc:
            found[prefix or "<module>"] = " ".join(doc.split())
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, f"{prefix}.{child.name}" if prefix else child.name)

    walk(tree, "")
    return found


def _docstring_lines(tree: ast.AST) -> set[int]:
    """Line numbers that belong to a docstring."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                inside.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return inside


def _outside_docstrings(text: str, tree: ast.AST) -> str:
    """The file without its docstrings (and without `pass` lines, which a docstring may replace):
    the code and comments a docs job must leave exactly as they were."""
    inside = _docstring_lines(tree)
    return "\n".join(line.rstrip() for n, line in enumerate(text.splitlines(), 1)
                     if n not in inside and line.strip() != "pass")


def docs_only_change(old: str, new: str) -> tuple[bool, str]:
    """Whether `new` differs from `old` only by added or extended docstrings: the same code (by
    syntax tree), every old docstring kept, and every line outside the docstrings, comments
    included, still there as it was (a model that reflows the file or drops the comments
    leaves the tree unchanged, so the tree alone can't see it)."""
    try:
        old_tree, new_tree = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return False, "it no longer parses"
    if ast.dump(_StripDocstrings().visit(ast.parse(old))) != ast.dump(_StripDocstrings().visit(ast.parse(new))):
        return False, "it changes code, not just docstrings"
    new_docs = _docstrings(new_tree)
    for name, text in _docstrings(old_tree).items():
        if text not in new_docs.get(name, ""):
            return False, f"it removes or rewrites the existing docstring of {name}"
    if not no_lines_removed(_outside_docstrings(old, old_tree), _outside_docstrings(new, new_tree)):
        return False, "it changes or removes lines outside the docstrings (comments or formatting)"
    return True, ""


def no_lines_removed(old: str, new: str) -> bool:
    ops = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes()
    return all(tag in ("equal", "insert") for tag, *_ in ops)


def verify_docs(ctx: StepContext) -> str:
    if not apply_candidate(ctx.workdir, ctx.state_dir / "docs.patch"):
        return "No documentation changes were generated; nothing to keep."
    notes, kept = [], 0
    for code, path in changed_files(ctx.workdir):
        file = ctx.workdir / path
        new = source_text(file.read_bytes()) if file.exists() else ""
        original = head_bytes(ctx.workdir, path)
        old = None if original is None else source_text(original)
        if path.endswith(".py") and old is not None:
            ok, why = docs_only_change(old, new)
        elif path.lower().endswith(DOC_FILES):
            ok, why = (True, "") if old is None else (no_lines_removed(old, new), "it removes or changes existing lines")
        else:
            ok, why = False, "docs jobs only change docstrings and documentation files"
        if ok:
            if original is not None and file.exists():
                match_file_style(file, original)
            kept += 1
            notes.append(f"kept {path}")
        else:
            revert(ctx.workdir, code, path)
            notes.append(f"reverted {path}: {why}")
    return f"Kept documentation changes in {kept} file(s).\n" + "\n".join(f"- {n}" for n in notes)


# --- review --------------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "You are a careful senior engineer reviewing code in a developer's own repository. "
    "Only report problems you can point to in the listing; never invent code or line numbers."
)
REVIEW_ASK = """Review the code below. Each line is prefixed with its file path and line number.
Write a Markdown report with these sections:
## Likely bugs
## Risky code
## Suggestions
For every point give the location as `path:line`, explain the problem in one or two sentences, and suggest a fix.
If a section has nothing worth reporting, write "Nothing found." under it.{note}

{listing}"""


def review_messages(listing: str, note: str | None = None) -> list[dict]:
    return [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": REVIEW_ASK.format(listing=listing, note=f"\nThe developer adds: {note}" if note else "")},
    ]


def _source_files(root: Path, target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(
        p for p in target.rglob("*")
        if p.is_file() and p.stat().st_size <= MAX_FILE_BYTES
        and not any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(target).parts)
    )


MIN_CUT_CHARS = 2_000  # a file is cut to the room left only when that room is worth reviewing


def numbered_listing(root: Path, target: str, max_chars: int) -> tuple[str, list[str]]:
    """The target's text files with `path:line|` prefixes, capped; also returns what was left out.
    A file that doesn't fit is cut at the last whole line that does (noted as "cut after line
    N") when that is worth reviewing, or when it is the first file, so one big file is reviewed
    in part rather than not at all; the rest are left out whole."""
    parts, omitted, used = [], [], 0
    for file in _source_files(root, root / target):
        data = file.read_bytes()
        if b"\0" in data:
            continue
        rel = file.relative_to(root).as_posix()
        block = "".join(f"{rel}:{n}| {line}\n" for n, line in enumerate(source_text(data).splitlines(), 1))
        if used + len(block) > max_chars:
            room = max_chars - used
            if room >= MIN_CUT_CHARS or not parts:
                cut = block[:room]
                cut = cut[: cut.rfind("\n") + 1]  # whole lines only
                if cut:
                    parts.append(cut)
                    used += len(cut)
                    omitted.append(f"{rel} (cut after line {cut.count(chr(10))})")
                    continue
            omitted.append(rel)
            continue
        parts.append(block)
        used += len(block)
    return "\n".join(parts), omitted


def review(ctx: StepContext) -> str:
    job, cfg = ctx.job, ctx.config
    listing, omitted = numbered_listing(ctx.workdir, job.target, cfg.review_max_chars)
    if not listing:
        raise BharatCodeError(f"nothing readable to review in {job.target}")
    estimated_prompt = len(listing) // 3 + 500
    max_tokens = min(cfg.review_max_tokens, ctx.budget_remaining() - estimated_prompt)
    if max_tokens < 500:
        raise BudgetExceeded("not enough token budget left for the review")
    result = ctx.chat(review_messages(listing, job.note), max_tokens=max_tokens, reasoning_effort="none")
    report = [
        f"# Code review: `{job.target}`",
        "",
        f"_Written by spare-gpu job `{job.id}` with BharatCode on "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. It can be wrong: check each point before acting on it._",
        "",
        result.content.strip(),
    ]
    if result.truncated:
        report += ["", "> **Note:** this review was cut off at the output token limit, so it is incomplete."]
    if omitted:
        report += ["", "> **Not reviewed** (over the size limit): " + ", ".join(f"`{p}`" for p in omitted)]
    name = f"gtu-review-{job.id}.md"
    (ctx.workdir / name).write_text("\n".join(report) + "\n", encoding="utf-8")
    return f"Wrote {name} ({result.usage.total_tokens} tokens" + (", cut off" if result.truncated else "") + ")."


# --- the project's test command, run whole (fix, green and implement) ----------------------

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


MAX_TEST_OUTPUT_CHARS = 12_000  # of failing test output in a prompt: the end, where pytest's summary is
TAIL_CHARS = 800  # of it in a verify step's summary, for the user


def _tail(text: str, limit: int) -> str:
    """The last `limit` characters of `text`, whole lines only, saying how much was cut."""
    text = text.strip() or "(the test command printed nothing)"
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    kept = kept[kept.find("\n") + 1:] if "\n" in kept else kept
    return f"[... {len(text) - len(kept):,} characters cut; the end of the output follows]\n{kept}"


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


# --- green (make the tests pass) -----------------------------------------------------------

GREEN_PROMPT = """Make the tests pass in this repository. Running `{test_command}` fails right now; its output is at the end of this message.
- The code to fix is in {where}. The tests say what the code should do: fix the code, not the tests.
- Do not modify or delete any existing test file. You may add new tests.
- Keep every change small and local; do not change behaviour beyond what the failing tests need.
- You may run `{test_command}` to check your work. Do not run any other commands.{note}
When you are finished, reply with one line saying what you changed and whether the tests pass now.

Test output:
{output}"""

GREEN_STATE = "green.json"  # in the job's state folder: the verify steps counted as rounds so far


def _revert_existing_tests(ctx: StepContext, kind: str) -> list[str]:
    """Undo every change to an existing test file (a job may not pass by weakening the tests);
    new test files stay. Returns a note per reverted file."""
    notes = []
    for code, path in changed_files(ctx.workdir):
        if TEST_FILE.search(path) and code.strip() != "??":
            revert(ctx.workdir, code, path)
            notes.append(f"reverted {path}: {kind} job may not change existing tests")
    return notes


def _base_commit(ctx: StepContext) -> str:
    """The commit the job's copy started from (its first, when the job doesn't record one)."""
    return ctx.job.base_commit or _git(ctx.workdir, "rev-list", "--max-parents=0", "HEAD").split()[0]


def _changed_since_start(ctx: StepContext) -> list[str]:
    """Files the job has changed in its copy since it started: saved in earlier steps or still
    uncommitted now (deleted files left out)."""
    saved = _git(ctx.workdir, "diff", "--name-only", "--diff-filter=d", "-z", _base_commit(ctx), "HEAD").split("\0")
    now = [p for code, p in changed_files(ctx.workdir) if code.strip() != "D"]
    return sorted({*filter(None, saved), *now})


def restore_start(ctx: StepContext) -> None:
    """Put the job's copy back to the files it started from, without touching its history: the
    index and working tree are read from the starting commit, then what is left untracked goes.
    The step's commit then records the undoing, and the copy's diff since the start is empty."""
    _git(ctx.workdir, "read-tree", "-u", "--reset", _base_commit(ctx))
    _git(ctx.workdir, "clean", "-fdq")


def _green_round(ctx: StepContext) -> int:
    """This verify step's round number (1-based). The counted steps are kept in a small file in
    the job's state folder, so a step run again (after a crash, or the router's one fallback
    rerun, which makes every step pending again) keeps the number it had."""
    path = ctx.state_dir / GREEN_STATE
    try:
        counted = [int(x) for x in json.loads(path.read_text(encoding="utf-8")).get("rounds") or []]
    except (OSError, ValueError, TypeError, AttributeError):
        counted = []
    if ctx.step_id not in counted:
        counted.append(ctx.step_id)
        path.write_text(json.dumps({"rounds": counted}), encoding="utf-8")
    return counted.index(ctx.step_id) + 1


def write_green(ctx: StepContext) -> str:
    """Run the test command in the copy; if it fails, have a Desktop session fix the code. The copy holds
    what earlier rounds kept, so each round starts from the last one's result."""
    job = ctx.job
    patch = ctx.state_dir / "green.patch"
    run = run_test_command(ctx)
    if run.ok:
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_bytes(b"")  # nothing for verify to apply (an older round's patch must not be)
        return "Tests already pass; nothing to do."
    where = "this repository" if job.target in (".", "") else f"`{job.target}`"
    prompt = GREEN_PROMPT.format(
        where=where, test_command=" ".join(ctx.config.test_command), output=_tail(run.output, MAX_TEST_OUTPUT_CHARS),
        note=f"\n- The developer adds: {job.note}" if job.note else "",
    )
    result = run_agent(ctx, prompt, edit=True, commands=ctx.config.allowed_commands)
    saved = save_candidate(ctx.workdir, patch)
    return _agent_summary(result, saved)


def verify_green(ctx: StepContext) -> str:
    """Round N of M. Apply what the session wrote, undo changes to existing tests, run the test
    command. Passing: done, and the changes of every round are kept. Failing with rounds left: a
    write and a verify step are added, so the scheduler runs another round from this state.
    Failing on the last round: the copy goes back to its starting files, so nothing is exported."""
    n, total = _green_round(ctx), ctx.config.green_rounds
    apply_candidate(ctx.workdir, ctx.state_dir / "green.patch")
    notes = _revert_existing_tests(ctx, "a green")
    run = run_test_command(ctx)
    shown = "".join(f"\n- {note}" for note in notes)
    if run.ok:
        changed = _changed_since_start(ctx)
        if not changed:
            return "Tests already pass; nothing to do." + shown
        return f"Tests pass after {n} round(s). Changed {len(changed)} file(s): {', '.join(changed)}." + shown
    if n < total:
        if not any(s["status"] == "pending" for s in ctx.store.steps(ctx.job.id)):
            ctx.store.add_step(ctx.job.id, "write")
            ctx.store.add_step(ctx.job.id, "verify")
        return (f"Round {n} of {total}: tests still fail; trying again." + shown
                + f"\nTest output ({run.why}):\n{_tail(run.output, TAIL_CHARS)}")
    restore_start(ctx)
    return (f"Tests still fail after {n} round(s); nothing kept." + shown
            + f"\n- discarded every change: the test command still fails ({run.why})"
            + f"\nTest output:\n{_tail(run.output, TAIL_CHARS)}")


# --- implement (a Markdown plan file) ------------------------------------------------------

IMPLEMENT_PROMPT = """Implement the plan below in this repository. The plan is the file `{target}`.
- Do what the plan asks, and nothing more: keep every change small and local.
- Do not modify or delete existing test files. Add tests for new behaviour where the plan asks for them.
- You may run `{test_command}` to check your work. Do not run any other commands.{note}
When you are finished, reply with one line per item of the plan: done, or why not.

The plan:
{plan}"""

PLAN_SUFFIXES = (".md", ".markdown")
MAX_PLAN_CHARS = 40_000


def plan_file_problem(path: Path, target: str) -> str | None:
    """Why `target` can't be an implement job's plan, or None: it must be a Markdown file."""
    target = Path(target).as_posix()  # as the user typed it, with forward slashes, on Windows too
    if not path.is_file():
        return f"{target} is not a file; an implement job needs a Markdown plan file (.md) in the project"
    if path.suffix.lower() not in PLAN_SUFFIXES:
        return f"{target} is not a Markdown file; an implement job needs a plan file ending in .md"
    return None


def write_implement(ctx: StepContext) -> str:
    job = ctx.job
    file = ctx.workdir / job.target
    problem = plan_file_problem(file, job.target)
    if problem:
        raise BharatCodeError(problem)
    plan = source_text(file.read_bytes()).strip()
    if not plan:
        raise BharatCodeError(f"{job.target} is empty; an implement job needs the plan's text")
    if len(plan) > MAX_PLAN_CHARS:
        plan = plan[:MAX_PLAN_CHARS] + f"\n[... cut here; the rest is in `{job.target}`]"
    prompt = IMPLEMENT_PROMPT.format(
        target=job.target, plan=plan, test_command=" ".join(ctx.config.test_command),
        note=f"\n- The developer adds: {job.note}" if job.note else "",
    )
    result = run_agent(ctx, prompt, edit=True, commands=ctx.config.allowed_commands)
    saved = save_candidate(ctx.workdir, ctx.state_dir / "implement.patch")
    return _agent_summary(result, saved)


def verify_implement(ctx: StepContext) -> str:
    """Keep the implementation only if the project's test command passes with it; changes to
    existing test files are reverted first."""
    if not apply_candidate(ctx.workdir, ctx.state_dir / "implement.patch"):
        return "Nothing was implemented; nothing kept."
    notes = _revert_existing_tests(ctx, "an implement")
    changed = [p for _, p in changed_files(ctx.workdir)]
    shown = "".join(f"\n- {note}" for note in notes)
    if not changed:
        return "Nothing was implemented; nothing kept." + shown
    run = run_test_command(ctx)
    if not run.ok:
        worktree.discard_uncommitted(ctx.workdir)
        return (f"Tests fail after implementing the plan; nothing kept." + shown
                + f"\n- discarded the changes: the test command failed with them ({run.why})"
                + f"\nTest output:\n{_tail(run.output, TAIL_CHARS)}")
    return (f"Implemented the plan; tests pass. Kept {len(changed)} file(s)."
            + "".join(f"\n- kept {p}" for p in changed) + shown)


PLANS = {
    "tests": [("write", write_tests), ("verify", verify_tests)],
    "docs": [("write", write_docs), ("verify", verify_docs)],
    "review": [("review", review)],
    "fix": [("write", write_fix), ("verify", verify_fix)],
    "green": [("write", write_green), ("verify", verify_green)],
    "implement": [("write", write_implement), ("verify", verify_implement)],
}
