"""Git isolation for jobs: each job works in its own worktree on branch gtu/<job-id>.

The user's checkout is only ever read (status, HEAD). Worktrees live under
<repo>/.gtu/worktrees/, which gitignores itself, so creating one never makes the
repo look dirty. Commits go to the job's branch only; nothing is ever pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BRANCH_PREFIX = "gtu/"
# Files that running code or tests leaves behind; never part of a job's output, even in repos
# whose .gitignore doesn't cover them.
GENERATED = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**")
# Commits made by jobs are attributed to the tool, so they are easy to spot in review.
AUTHOR = ("-c", "user.name=spare-gpu", "-c", "user.email=spare-gpu@localhost")


class GitError(Exception):
    """A git operation was refused or failed; the message is meant for the user."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
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


def ensure_clean(repo: Path) -> None:
    """Refuse to start work on a repo with uncommitted changes or untracked files."""
    repo_root(repo)
    status = _git(Path(repo), "status", "--porcelain").stdout.strip()
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


def create(repo: Path, job_id: str) -> Path:
    """The job's worktree, creating it (and its branch from the current HEAD) if needed."""
    repo = Path(repo)
    path = worktree_path(repo, job_id)
    branch = branch_name(job_id)
    if path.exists() and (path / ".git").exists():
        return path  # resuming
    ensure_clean(repo)
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
            proc = subprocess.run(["git", *args], cwd=repo, env=env, input=stdin, capture_output=True)
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


def remove(repo: Path, job_id: str) -> None:
    """Delete the job's worktree folder. The branch, and every commit on it, is kept for review."""
    repo = Path(repo)
    path = worktree_path(repo, job_id)
    if path.exists() and (path / ".git").exists():
        discard_uncommitted(path)
        _git(repo, "worktree", "remove", str(path))
    _git(repo, "worktree", "prune")
