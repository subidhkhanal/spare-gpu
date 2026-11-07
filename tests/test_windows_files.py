"""Real jobs (fake CLI, real git and pytest) under the conditions Windows users actually have:
spaces and non-ASCII in paths, long paths, CRLF line endings, byte-order marks, locked files."""

import difflib
import json
import sys
from pathlib import Path

import pytest

from gpu_token_utilization import worktree
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig, numbered_listing
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

BOM = "﻿"
SOURCE = "def add(a, b):\n    return a + b\n"
DOCUMENTED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def make_repo(root, files: dict[str, bytes], autocrlf="false"):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", autocrlf)
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def run_job(store, client, fake, scenario):
    def run(repo, kind, target, writes=None, review="## Likely bugs\nNothing found."):
        desktop = scenario(steps=[{"tokens": 1000, "writes": writes or {}}])
        fake.queue(Reply(content=review))
        job = store.add_job(kind, target, str(repo))
        logs = []
        Scheduler(store, client, agent=desktop.agent(timeout=60),
                  config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                  sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
        return store.get_job(job.id), store.steps(job.id), logs

    return run


def out(job, path) -> bytes:
    """A file as the job left it in spare-gpu/<date>_<time>/."""
    return (Path(job.repo) / job.output_dir / path).read_bytes()


def produced(job) -> set[str]:
    if not job.output_dir:
        return set()
    root = Path(job.repo) / job.output_dir
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def deletions(job, path) -> int:
    """Lines of the user's file that the job's version no longer has, byte for byte."""
    before = (Path(job.repo) / path).read_bytes().splitlines(keepends=True)
    after = out(job, path).splitlines(keepends=True)
    return sum(1 for line in difflib.ndiff(before, after) if line.startswith("- "))


# --- paths with spaces and non-ASCII names ---------------------------------------------------

@pytest.mark.parametrize("kind", ["tests", "docs", "review"])
def test_every_job_kind_works_in_a_path_with_spaces(kind, tmp_path, run_job):
    repo = make_repo(tmp_path / "My Name" / "my repo", {"src/my calc.py": SOURCE.encode()})
    writes = {
        "tests": {"tests/test_my_calc.py": "import importlib.util, pathlib\n"
                  "p = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'my calc.py'\n"
                  "spec = importlib.util.spec_from_file_location('calc', p); m = importlib.util.module_from_spec(spec)\n"
                  "spec.loader.exec_module(m)\n\n\ndef test_add():\n    assert m.add(1, 2) == 3\n"},
        "docs": {"src/my calc.py": DOCUMENTED},
        "review": {},
    }[kind]
    job, steps, logs = run_job(repo, kind, "src/my calc.py", writes)
    assert job.status == "done", "\n".join(logs) + json.dumps(steps, default=str)
    expected = {"tests": "tests/test_my_calc.py", "docs": "src/my calc.py", "review": f"gtu-review-{job.id}.md"}[kind]
    assert expected in produced(job)
    if kind == "docs":
        assert b"Return the sum" in out(job, "src/my calc.py")
    assert git(repo, "status", "--porcelain") == ""


def test_docs_job_on_a_non_ascii_file_name(tmp_path, run_job):
    repo = make_repo(tmp_path / "repo", {"src/café.py": SOURCE.encode()})
    job, steps, logs = run_job(repo, "docs", "src/café.py", {"src/café.py": DOCUMENTED})
    assert job.status == "done", "\n".join(logs)
    assert "kept src/café.py" in steps[1]["output"], steps[1]["output"]


# --- byte-order marks and line endings -------------------------------------------------------

def test_docs_job_on_a_file_with_a_byte_order_mark(tmp_path, run_job):
    # PowerShell 5.1's `Set-Content -Encoding utf8` and some editors write one; Python's parser
    # rejects it in a str, so these files used to be reverted as "no longer parses"
    repo = make_repo(tmp_path / "repo", {"src/calc.py": (BOM + SOURCE).encode()})
    job, steps, logs = run_job(repo, "docs", "src/calc.py", {"src/calc.py": BOM + DOCUMENTED})
    assert "kept src/calc.py" in steps[1]["output"], steps[1]["output"]
    assert out(job, "src/calc.py").startswith(BOM.encode())


def test_docs_job_keeps_the_files_crlf_and_bom_when_the_cli_drops_them(tmp_path, run_job):
    # a CRLF file stored as CRLF (autocrlf off), rewritten by the CLI with LF and no BOM: the
    # result must show only the added docstring, not every line changed
    original = (BOM + SOURCE).replace("\n", "\r\n").encode()
    repo = make_repo(tmp_path / "repo", {"src/calc.py": original})
    job, steps, logs = run_job(repo, "docs", "src/calc.py", {"src/calc.py": DOCUMENTED})
    assert "kept src/calc.py" in steps[1]["output"], steps[1]["output"]
    kept = out(job, "src/calc.py")
    assert kept.startswith(BOM.encode()) and b"\r\n" in kept and b"\n" not in kept.replace(b"\r\n", b"")
    assert deletions(job, "src/calc.py") == 0


def test_docs_job_with_autocrlf_on_shows_only_added_lines(tmp_path, run_job):
    # Git for Windows' default: LF in the repo, CRLF in the worktree
    repo = make_repo(tmp_path / "repo", {"src/calc.py": SOURCE.encode(), "README.md": b"# Calc\n\nAdds.\n"},
                     autocrlf="true")
    job, steps, logs = run_job(repo, "docs", "src/calc.py",
                               {"src/calc.py": DOCUMENTED, "README.md": "# Calc\n\nAdds.\n\nUsage: add(1, 2).\n"})
    assert job.status == "done", "\n".join(logs)
    assert "kept src/calc.py" in steps[1]["output"] and "kept README.md" in steps[1]["output"], steps[1]["output"]
    assert deletions(job, "src/calc.py") == 0 and deletions(job, "README.md") == 0
    assert b"\r\n" not in out(job, "src/calc.py")  # LF, like the user's file


def test_review_listing_does_not_show_the_byte_order_mark(tmp_path):
    (tmp_path / "calc.py").write_bytes((BOM + SOURCE).replace("\n", "\r\n").encode())
    listing, _ = numbered_listing(tmp_path, "calc.py", 10_000)
    assert BOM not in listing and "\r" not in listing
    assert listing.startswith("calc.py:1| def add(a, b):")


# --- long paths ------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows MAX_PATH")
def test_a_job_whose_worktree_paths_pass_260_characters(tmp_path, run_job):
    # the worktree adds ".gtu/worktrees/<id>/" (24 characters) to every path in the repo, so a
    # repo that is fine where it is can go past Windows' old 260-character limit in its worktree
    root = tmp_path / "repo"
    deep = "/".join(["d" * 20] * 20)
    room = 250 - len(str(root)) - 1
    rel = f"{deep}"[: room - len("/calc.py")].rstrip("/") + "/calc.py"
    assert len(str(root)) + 1 + len(rel) < 260 < len(str(root)) + 25 + len(rel)
    repo = make_repo(root, {"src/calc.py": SOURCE.encode(), rel: SOURCE.encode()})
    job, steps, logs = run_job(repo, "review", "src/calc.py")
    assert job.status == "done", "\n".join(logs)


def test_without_windows_long_paths_a_too_long_repo_gets_a_fix_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "_long_paths_enabled", lambda: False)
    root = tmp_path / "repo"
    rel = "/".join(["d" * 20] * 12)[: 250 - len(str(root))].rstrip("/") + "/x.py"
    repo = make_repo(root, {rel: SOURCE.encode()})
    with pytest.raises(worktree.GitError, match="(?s)longer than Windows allows.*LongPathsEnabled"):
        worktree.create(repo, "toolong")
    assert not worktree.worktree_path(repo, "toolong").exists()  # refused before copying anything
    short = make_repo(tmp_path / "short", {"x.py": SOURCE.encode()})
    assert worktree.create(short, "fine").exists()


# --- files held open (antivirus scanning, an editor) ----------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete open files")
def test_a_file_held_open_for_a_moment_does_not_leave_the_worktree_behind(git_repo, monkeypatch):
    wt = worktree.create(git_repo, "lockedjob")
    handle = open(wt / "src" / "calc.py", "rb")  # Windows won't delete a file someone has open
    waits = []

    def scanner_lets_go(seconds):  # as Defender does a moment after a file was written
        waits.append(seconds)
        handle.close()

    monkeypatch.setattr(worktree.time, "sleep", scanner_lets_go)
    try:
        worktree.remove(git_repo, "lockedjob")
    finally:
        handle.close()
    assert waits and not wt.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete open files")
def test_a_file_held_open_for_good_gives_a_clear_message(git_repo, monkeypatch):
    wt = worktree.create(git_repo, "stuckjob")
    monkeypatch.setattr(worktree.time, "sleep", lambda seconds: None)
    with open(wt / "src" / "calc.py", "rb"):
        with pytest.raises(worktree.GitError, match="(?s)holding a file .*delete that folder"):
            worktree.remove(git_repo, "stuckjob")
