"""Isolation for jobs: each job works on its own private copy of the project.

The project can be any folder: a git repo or not, with or without uncommitted changes. When a
job starts, spare-gpu copies the project's files, as they are right now, into
<project>/.gtu/worktrees/<job-id>/ and keeps a private git history there, so steps can be
checked, undone and resumed. git is only a tool here: the project itself is never treated as a
repo, and the user's own files are never written.

When the job finishes, the files it changed are copied to <project>/spare-gpu/<date>_<time>/,
keeping their paths, and the private copy is deleted. No branch, commit or history is left in
the project. Both spare-gpu folders ignore themselves in git, in case the project is a repo.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Files that running code or tests leaves behind; never part of a job's output.
GENERATED = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**")
# Folders never copied into a job's copy: spare-gpu's own, version control, and the usual
# dependency and cache folders (large, and not something a job should read or change).
SKIP_DIRS = {".git", ".gtu", "spare-gpu", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}
# The private history's commits are attributed to the tool.
AUTHOR = ("-c", "user.name=spare-gpu", "-c", "user.email=spare-gpu@localhost")
# Every git call: ".gtu/worktrees/<id>/" lengthens each path, which can pass Windows' 260-character
# limit (core.longpaths; other platforms ignore it), and file names come back as real UTF-8
# rather than octal escapes (core.quotepath).
GIT = ("git", "-c", "core.longpaths=true", "-c", "core.quotepath=false")
WINDOWS_PATH_LIMIT = 259
REMOVE_ATTEMPTS, REMOVE_RETRY_DELAY = 4, 1.5  # antivirus or an editor can hold files for a moment
OUTPUT_DIR = "spare-gpu"  # <project>/spare-gpu/<date>_<time>/: each finished job's edited files
# spare-gpu's own folders, for git commands run in a project that happens to be a repo.
OWN_FILES = ("--", ".", ":(exclude).gtu", f":(exclude){OUTPUT_DIR}")


class GitError(Exception):
    """A git operation was refused or failed; the message is meant for the user."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            [*GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise GitError("git isn't installed: spare-gpu uses it to keep each job's copy (your project "
                       "doesn't need to be a git repo). Install it from https://git-scm.com") from None
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def worktree_path(repo: Path, job_id: str) -> Path:
    """The job's private copy of the project."""
    return Path(repo) / ".gtu" / "worktrees" / job_id


def repo_root(path: Path) -> Path:
    """The project folder for `path`: the top of the git repo it's in, if any, else the folder
    itself. Any folder can be a project."""
    path = Path(path)
    if not path.is_dir():
        raise GitError(f"{path} is not a folder")
    proc = _git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else path


def ensure_clean(repo: Path) -> None:
    """Check the project can be worked on. Any folder can: uncommitted changes are fine, since
    each job copies the files as they are."""
    if not Path(repo).is_dir():
        raise GitError(f"{repo} is not a folder")


def _ignore_own_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")


def _is_repo_top(repo: Path) -> bool:
    proc = _git(repo, "rev-parse", "--show-toplevel", check=False)
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == Path(repo).resolve()


def project_files(repo: Path) -> list[str]:
    """The project's files as the user sees them, relative, with "/" separators. In a git repo,
    what git shows (tracked files plus new ones, minus what .gitignore hides); elsewhere every
    file, minus SKIP_DIRS."""
    repo = Path(repo)
    if _is_repo_top(repo):
        names = _git(repo, "ls-files", "-co", "--exclude-standard", "-z").stdout.split("\0")
        return sorted({n for n in names if n and n.split("/")[0] not in SKIP_DIRS and (repo / n).is_file()})
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(repo)
        found += [(rel / f).as_posix() for f in files]
    return sorted(found)


def _long_paths_enabled() -> bool:
    """Windows' LongPathsEnabled switch. Git copes either way with core.longpaths, but Python,
    pytest and BharatCode can only open longer paths when it is on."""
    if sys.platform != "win32":
        return True
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _check_path_lengths(files: list[str], path: Path) -> None:
    """Refuse up front, with a fix, rather than fail halfway when the copy's paths would be too
    long for Windows."""
    if _long_paths_enabled():
        return
    longest = max(files, key=len, default="")
    if len(str(path)) + 1 + len(longest) <= WINDOWS_PATH_LIMIT:
        return
    raise GitError(
        f"the job's copy of this project would have paths longer than Windows allows (260 characters), "
        f"e.g. {path}\\{longest}.\nEither move the project to a shorter folder (like C:\\code\\myproject), or "
        "turn on long paths once, in PowerShell run as Administrator:\n"
        "  New-ItemProperty -Path HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
        "-Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
    )


def create(repo: Path, job_id: str) -> Path:
    """The job's private copy, made from the project's current files if it doesn't exist yet."""
    repo = Path(repo)
    path = worktree_path(repo, job_id)
    if path.exists() and (path / ".git").exists():
        return path  # resuming
    ensure_clean(repo)
    _ignore_own_folder(repo / ".gtu")
    files = project_files(repo)
    _check_path_lengths(files, path)
    if path.exists():  # a copy cut short (a crash while copying): start it again
        _rmtree(path)
    path.mkdir(parents=True)
    for name in files:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / name, target)
    _git(path, "init", "--quiet")
    # Byte-exact history: the files the user gets back must have exactly the line endings the job wrote.
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "config", "core.safecrlf", "false")
    _git(path, "add", "-A", "--", ".", *(f":(exclude,glob){g}" for g in GENERATED))
    _git(path, *AUTHOR, "commit", "--quiet", "--allow-empty", "-m", f"the project as it was when job {job_id} started")
    return path


def discard_uncommitted(worktree: Path) -> None:
    """Throw away unfinished edits in the job's copy (e.g. after a crash mid-step)."""
    _git(worktree, "reset", "--quiet", "--hard", "HEAD")
    _git(worktree, "clean", "-fdq")


def commit(worktree: Path, message: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Save everything changed in the job's copy, except `exclude` and generated caches, to its
    private history. Returns the commit id, or None if nothing changed."""
    pathspec = ["--", ".", *(f":(exclude){p}" for p in exclude), *(f":(exclude,glob){g}" for g in GENERATED)]
    _git(worktree, "add", "-A", *pathspec)
    if _git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(worktree, *AUTHOR, "commit", "--quiet", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def changed_files(worktree: Path, base: str | None) -> list[str]:
    """Files the job added or changed in its copy since it started (deleted files are left out:
    there's nothing to copy). Empty if the copy is gone or nothing changed."""
    worktree = Path(worktree)
    if not (worktree / ".git").exists():
        return []
    base = base or _git(worktree, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    out = _git(worktree, "diff", "--name-only", "--diff-filter=d", "-z", base, "HEAD").stdout
    return [name for name in out.split("\0") if name]


def export(repo: Path, job_id: str, base: str | None, when: datetime) -> Path | None:
    """Copy the files the job changed, as it saved them, into <project>/spare-gpu/<date>_<time>/
    with their paths. Returns that folder, or None if the job changed nothing. Call it before
    remove(): it reads the job's copy."""
    repo = Path(repo)
    copy = worktree_path(repo, job_id)
    files = changed_files(copy, base)
    if not files:
        return None
    root = repo / OUTPUT_DIR
    _ignore_own_folder(root)  # never part of the user's repo, if it is one
    stamp = when.strftime("%Y-%m-%d_%H-%M-%S")
    folder, n = root / stamp, 1
    while folder.exists():  # two jobs finishing in the same second
        n += 1
        folder = root / f"{stamp}-{n}"
    folder.mkdir()
    for name in files:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run([*GIT, "show", f"HEAD:{name}"], cwd=copy, capture_output=True)
        if blob.returncode != 0:
            raise GitError(f"couldn't read {name} from the job's copy: {blob.stderr.decode('utf-8', 'replace')[:200]}")
        target.write_bytes(blob.stdout)  # bytes: exactly what the job wrote, line endings included
    return folder


def remove(repo: Path, job_id: str) -> None:
    """Delete the job's copy, private history and all. Nothing of it is left in the project."""
    path = worktree_path(Path(repo), job_id)
    if path.exists():
        _rmtree(path)


def _rmtree(path: Path) -> None:
    # Windows won't delete a file another program has open; antivirus scanners (Defender) and
    # editors often hold one for a moment right after it was written.
    for attempt in range(1, REMOVE_ATTEMPTS + 1):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_make_writable_and_retry)
            else:
                shutil.rmtree(path, onerror=_make_writable_and_retry)
            return
        except OSError as exc:
            if attempt == REMOVE_ATTEMPTS:
                raise GitError(f"{exc}\nA program (often antivirus, or an editor) is holding a file in "
                               f"{path}. Close it and delete that folder.") from None
            time.sleep(REMOVE_RETRY_DELAY)


def _make_writable_and_retry(func, path, exc) -> None:
    """shutil.rmtree helper: git marks object files read-only, which Windows won't delete."""
    os.chmod(path, stat.S_IWRITE)
    func(path)
