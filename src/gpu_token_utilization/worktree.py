"""Git isolation for jobs: each job works in its own worktree on branch gtu/<job-id>.

The user's checkout is only ever read (status, HEAD). Worktrees live under
<repo>/.gtu/worktrees/, which gitignores itself, so creating one never makes the
repo look dirty. Commits go to the job's branch only; nothing is ever pushed.

What the user gets is a plain folder: when a job finishes, the files it changed are copied to
<repo>/spare-gpu/<date>_<time>/, keeping their paths. That folder gitignores itself too, so
the repo stays clean; the branch stays behind as spare-gpu's own record.
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

BRANCH_PREFIX = "gtu/"
# Files that running code or tests leaves behind; never part of a job's output, even in repos
# whose .gitignore doesn't cover them.
GENERATED = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**")
# Commits made by jobs are attributed to the tool, so they are easy to spot in review.
AUTHOR = ("-c", "user.name=spare-gpu", "-c", "user.email=spare-gpu@localhost")
# Every git call: worktrees add ".gtu/worktrees/<id>/" to each path, which can push a repo past
# Windows' 260-character limit (core.longpaths; other platforms ignore it), and file names
# come back as real UTF-8 rather than octal escapes (core.quotepath).
GIT = ("git", "-c", "core.longpaths=true", "-c", "core.quotepath=false")
WINDOWS_PATH_LIMIT = 259
REMOVE_ATTEMPTS, REMOVE_RETRY_DELAY = 4, 1.5  # antivirus or an editor can hold files for a moment


class GitError(Exception):
    """A git operation was refused or failed; the message is meant for the user."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [*GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def branch_name(job_id: str) -> str:
    return f"{BRANCH_PREFIX}{job_id}"


def worktree_path(repo: Path, job_id: str) -> Path:
    return Path(repo) / ".gtu" / "worktrees" / job_id


def repo_root(path: Path) -> Path:
    proc = _git(Path(path), "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise GitError(f"{path} is not inside a git repository")
    return Path(proc.stdout.strip())


# spare-gpu's own folder never counts as a change to the user's repo. It normally ignores itself
# (.gtu/.gitignore), but a user can create it first, e.g. to write .gtu/switch-safe.
OUTPUT_DIR = "spare-gpu"  # <repo>/spare-gpu/<date>_<time>/: each finished job's edited files
OWN_FILES = ("--", ".", ":(exclude).gtu", f":(exclude){OUTPUT_DIR}")


def ensure_clean(repo: Path) -> None:
    """Refuse to start work on a repo with uncommitted changes or untracked files."""
    repo_root(repo)
    status = _git(Path(repo), "status", "--porcelain", *OWN_FILES).stdout.strip()
    if status:
        shown = "\n".join("  " + line for line in status.splitlines()[:10])
        raise GitError(
            "the repo has uncommitted changes; commit or stash them first, so the job's branch "
            f"starts from exactly what you see:\n{shown}"
        )
    if _git(Path(repo), "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        raise GitError("the repo has no commits yet; make one first")


def _ignore_gtu_folder(repo: Path) -> None:
    gtu = Path(repo) / ".gtu"
    gtu.mkdir(exist_ok=True)
    marker = gtu / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")


def _long_paths_enabled() -> bool:
    """Windows' LongPathsEnabled switch. Git copes either way with core.longpaths, but Python,
    pytest and the BharatCode CLI can only open longer paths when it is on."""
    if sys.platform != "win32":
        return True
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _check_path_lengths(repo: Path, path: Path) -> None:
    """Refuse up front, with a fix, rather than fail halfway when the worktree's paths would be
    too long for Windows."""
    if _long_paths_enabled():
        return
    files = _git(repo, "ls-files", "-z").stdout.split("\0")
    longest = max((f for f in files if f), key=len, default="")
    if len(str(path)) + 1 + len(longest) <= WINDOWS_PATH_LIMIT:
        return
    raise GitError(
        f"the job's copy of this repo would have paths longer than Windows allows (260 characters), "
        f"e.g. {path}\\{longest}.\nEither move the repo to a shorter folder (like C:\\code\\myrepo), or turn "
        "on long paths once, in PowerShell run as Administrator:\n"
        "  New-ItemProperty -Path HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
        "-Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
    )


def create(repo: Path, job_id: str) -> Path:
    """The job's worktree, creating it (and its branch from the current HEAD) if needed."""
    repo = Path(repo)
    path = worktree_path(repo, job_id)
    branch = branch_name(job_id)
    if path.exists() and (path / ".git").exists():
        return path  # resuming
    ensure_clean(repo)
    _check_path_lengths(repo, path)
    _ignore_gtu_folder(repo)
    _git(repo, "worktree", "prune")  # forget worktrees whose folders were deleted by hand
    if _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
        _git(repo, "worktree", "add", str(path), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")
    return path


def discard_uncommitted(worktree: Path) -> None:
    """Throw away unfinished edits in the job's own worktree (e.g. after a crash mid-step)."""
    _git(worktree, "reset", "--quiet", "--hard", "HEAD")
    _git(worktree, "clean", "-fdq")


def commit(worktree: Path, message: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Commit everything changed in the worktree except `exclude` and generated caches.
    Returns the commit id, or None if nothing changed."""
    pathspec = ["--", ".", *(f":(exclude){p}" for p in exclude), *(f":(exclude,glob){g}" for g in GENERATED)]
    _git(worktree, "add", "-A", *pathspec)
    if _git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(worktree, *AUTHOR, "commit", "--quiet", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def add_file_to_branch(repo: Path, branch: str, path: str, content: str, message: str) -> str | None:
    """Commit one file onto `branch` without checking anything out.

    Uses a throwaway index, so neither the user's checkout nor any working files are touched.
    The branch only moves if it is still where we read it (no force). Returns the new commit,
    or None if the file already has exactly this content.
    """
    import os
    import tempfile

    repo = Path(repo)
    parent = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def run(*args: str, stdin: bytes | None = None) -> str:
            # Bytes, not text: text-mode stdin would turn \n into \r\n on Windows.
            proc = subprocess.run([*GIT, *args], cwd=repo, env=env, input=stdin, capture_output=True)
            if proc.returncode != 0:
                raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
            return proc.stdout.decode("utf-8", "replace").strip()

        run("read-tree", parent)
        blob = run("hash-object", "-w", "--stdin", stdin=content.encode("utf-8"))
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = run("write-tree")
        if tree == run("rev-parse", f"{parent}^{{tree}}"):
            return None
        commit = run(*AUTHOR, "commit-tree", tree, "-p", parent, "-m", message)
        run("update-ref", f"refs/heads/{branch}", commit, parent)
    return commit


def changed_files(repo: Path, job_id: str, base: str | None) -> list[str]:
    """Files the job's branch added or changed since `base` (deleted files are left out: there's
    nothing to copy). Empty if the branch is gone or changed nothing."""
    branch = branch_name(job_id)
    if _git(Path(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode != 0:
        return []
    base = base or _git(Path(repo), "merge-base", "HEAD", branch).stdout.strip()
    out = _git(Path(repo), "diff", "--name-only", "--diff-filter=d", "-z", base, branch).stdout
    return [name for name in out.split("\0") if name]


def export(repo: Path, job_id: str, base: str | None, when: datetime) -> Path | None:
    """Copy the files the job changed, as they are on its branch, into
    <repo>/spare-gpu/<date>_<time>/ with their paths. Returns that folder, or None if the job
    changed nothing. Read straight from git, so it works with or without the worktree."""
    repo = Path(repo)
    files = changed_files(repo, job_id, base)
    if not files:
        return None
    root = repo / OUTPUT_DIR
    root.mkdir(exist_ok=True)
    marker = root / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")  # never part of the user's repo
    stamp = when.strftime("%Y-%m-%d_%H-%M-%S")
    folder, n = root / stamp, 1
    while folder.exists():  # two jobs finishing in the same second
        n += 1
        folder = root / f"{stamp}-{n}"
    folder.mkdir()
    branch = branch_name(job_id)
    for name in files:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run([*GIT, "show", f"{branch}:{name}"], cwd=repo, capture_output=True)
        if blob.returncode != 0:
            raise GitError(f"couldn't read {name} from {branch}: {blob.stderr.decode('utf-8', 'replace')[:200]}")
        target.write_bytes(blob.stdout)  # bytes: exactly what the job wrote, line endings included
    return folder


def remove(repo: Path, job_id: str) -> None:
    """Delete the job's worktree folder. The branch, and every commit on it, is kept for review."""
    repo = Path(repo)
    path = worktree_path(repo, job_id)
    if path.exists() and (path / ".git").exists():
        discard_uncommitted(path)
        # Windows won't delete a file another program has open; antivirus scanners (Defender)
        # and editors often hold one for a moment right after it was written.
        # git unregisters the worktree before deleting files, so after a failed attempt what's
        # left is a plain folder for us to delete.
        for attempt in range(1, REMOVE_ATTEMPTS + 1):
            try:
                if (path / ".git").exists():
                    _git(repo, "worktree", "remove", "--force", str(path))
                else:
                    if sys.version_info >= (3, 12):
                        shutil.rmtree(path, onexc=_make_writable_and_retry)
                    else:
                        shutil.rmtree(path, onerror=_make_writable_and_retry)
                break
            except (GitError, OSError) as exc:
                if attempt == REMOVE_ATTEMPTS:
                    raise GitError(f"{exc}\nA program (often antivirus, or an editor) is holding a file in "
                                   f"{path}. Close it and delete that folder; the branch is safe.") from None
                time.sleep(REMOVE_RETRY_DELAY)
    _git(repo, "worktree", "prune")


def _make_writable_and_retry(func, path, exc) -> None:
    """shutil.rmtree helper: git marks object files read-only, which Windows won't delete."""
    os.chmod(path, stat.S_IWRITE)
    func(path)
