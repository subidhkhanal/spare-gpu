"""The three job types and the steps they are made of.

tests:  write   - BharatCode's CLI writes new test files (it may run allowlisted test commands);
                  the result is saved as a patch outside the worktree, nothing is committed yet.
        verify  - we apply the patch, run each new test file ourselves with the allowlisted
                  test command, drop failing tests, discard files that still fail, revert any
                  change that is not a new test file, and commit what is left.
docs:   write   - the CLI adds docstrings and README text (no commands allowed); saved as a patch.
        verify  - we keep a Python file only if its code is unchanged apart from docstrings and no
                  existing docstring was removed or shortened, and a Markdown file only if no line
                  was removed; everything else is reverted. Then commit.
review: review  - our own client sends the code (with line numbers) and writes the answer to
                  gtu-review-<job-id>.md. No source file is changed.

Saving the CLI's work as a patch before checking it means a crash during the check never loses
it and never leaves unchecked work committed.
"""

from __future__ import annotations

import ast
import difflib
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
from .agent_cli import CONFIG_FILE, CREDENTIAL_RETRIES, CliNotFound, CliResult
from .api import env_without_key
from .budget import BudgetExceeded
from .client import BharatCodeError, BusyError
from .runstate import StopRequested

if TYPE_CHECKING:
    from .scheduler import StepContext

DEFAULT_ALLOWED = ("python -m pytest", "pytest", "npm test", "go test")
def _test_env() -> dict:
    """Model-written tests never see the API key, and leave no bytecode caches behind."""
    return env_without_key(PYTHONDONTWRITEBYTECODE="1")
SKIP_DIRS = {".git", ".gtu", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
MAX_FILE_BYTES = 200_000


@dataclass
class JobConfig:
    test_command: tuple[str, ...] = ("python", "-m", "pytest")
    allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED
    test_timeout: float = 300.0
    cli_timeout: float = 900.0
    review_max_chars: int = 40_000
    review_max_tokens: int = 4_000

    @classmethod
    def from_env(cls) -> JobConfig:
        cfg = cls()
        if os.environ.get("GTU_TEST_COMMAND"):
            cfg.test_command = tuple(shlex.split(os.environ["GTU_TEST_COMMAND"]))
        if os.environ.get("GTU_ALLOWED_COMMANDS"):
            cfg.allowed_commands = tuple(c.strip() for c in os.environ["GTU_ALLOWED_COMMANDS"].split(",") if c.strip())
        if os.environ.get("GTU_TEST_TIMEOUT"):
            cfg.test_timeout = float(os.environ["GTU_TEST_TIMEOUT"])
        if os.environ.get("GTU_CLI_TIMEOUT"):
            cfg.cli_timeout = float(os.environ["GTU_CLI_TIMEOUT"])
        cfg.check()
        return cfg

    def check(self) -> None:
        if not command_allowed(self.test_command, self.allowed_commands):
            raise ValueError(f"test command {' '.join(self.test_command)!r} is not in the allowlist {self.allowed_commands}")


def command_allowed(command: tuple[str, ...], allowed: tuple[str, ...]) -> bool:
    words = [Path(command[0]).stem.lower(), *command[1:]] if command else []
    return any(words[: len(a.split())] == a.split() for a in allowed)


# --- git helpers on the job's worktree -------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run([*worktree.GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise BharatCodeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def changed_files(workdir: Path) -> list[tuple[str, str]]:
    """(git status code, path) for every change in the worktree, except our CLI config and caches.
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
    """Give a rewritten file the original's byte-order mark and line endings. The CLI (and
    many editors) normalise both, which would make the branch's diff show every line changed."""
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


# --- the CLI step shared by tests and docs ---------------------------------------------

def run_cli(ctx: StepContext, prompt: str, *, edit: bool, commands: tuple[str, ...]) -> CliResult:
    if ctx.cli is None:
        raise CliNotFound("no BharatCode CLI is configured for this run")
    budget = ctx.budget_remaining()
    if budget <= 0:
        raise BudgetExceeded("token budget used up before the CLI could start")
    result = ctx.cli.run(
        ctx.workdir, prompt, edit=edit, commands=commands, token_budget=budget, on_step=ctx.log_cli_step,
        should_stop=ctx.should_stop,
        on_retry=lambda n: ctx.log(f"  BharatCode couldn't read its own sign-in; continuing the same session "
                                   f"(retry {n} of {CREDENTIAL_RETRIES})."),
    )
    if result.stopped == "stop":
        raise StopRequested("stopped by `gtu stop`")
    if result.stopped == "budget":
        raise BudgetExceeded(f"stopped the BharatCode CLI after {result.tokens} tokens: budget reached")
    if result.stopped == "timeout":
        raise BusyError(f"the BharatCode CLI did not finish within {ctx.cli.timeout:.0f} s")
    if not result.ok:
        raise BharatCodeError(f"the BharatCode CLI failed: {result.error or 'unknown error'}")
    return result


def _cli_summary(result: CliResult, saved: list[str]) -> str:
    return (
        f"BharatCode CLI: {len(result.steps)} step(s), {result.tokens} tokens; "
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
    result = run_cli(ctx, prompt, edit=True, commands=ctx.config.allowed_commands)
    saved = save_candidate(ctx.workdir, ctx.state_dir / "tests.patch")
    return _cli_summary(result, saved)


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
    lines = source.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1 : end]
    file.write_bytes("".join(lines).encode("utf-8"))
    match_file_style(file, original)  # same byte-order mark and line endings as the model wrote
    return len(spans)


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
    result = run_cli(ctx, prompt, edit=True, commands=())
    saved = save_candidate(ctx.workdir, ctx.state_dir / "docs.patch")
    return _cli_summary(result, saved)


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


def docs_only_change(old: str, new: str) -> tuple[bool, str]:
    """Whether `new` differs from `old` only by added or extended docstrings."""
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


def numbered_listing(root: Path, target: str, max_chars: int) -> tuple[str, list[str]]:
    """The target's text files with `path:line|` prefixes, capped; also returns files left out."""
    parts, omitted, used = [], [], 0
    for file in _source_files(root, root / target):
        data = file.read_bytes()
        if b"\0" in data:
            continue
        rel = file.relative_to(root).as_posix()
        block = "".join(f"{rel}:{n}| {line}\n" for n, line in enumerate(source_text(data).splitlines(), 1))
        if used + len(block) > max_chars:
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


PLANS = {
    "tests": [("write", write_tests), ("verify", verify_tests)],
    "docs": [("write", write_docs), ("verify", verify_docs)],
    "review": [("review", review)],
}
