"""Real jobs (fake Desktop, real git and pytest) under the conditions Windows users actually have:
spaces and non-ASCII in paths, long paths, locked files."""

import json
import sys
from pathlib import Path

import pytest

from gpu_token_utilization import worktree
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

SOURCE = "def add(a, b):\n    return a + b\n"
DOCUMENTED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
PASSING = b"def test_ok():\n    assert True\n"


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
def run_job(store, clock, scenario):
    def run(repo, target, writes=None):
        desktop = scenario(steps=[{"tokens": 1000, "writes": writes or {}}])
        job = store.add_job("fix", target, str(repo), "src/calc.py:1 add a docstring")
        logs = []
        Scheduler(store, agent=desktop.agent(timeout=60),
                  config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                  sleep=clock.sleep, now=clock.utcnow, log=logs.append).run_job(job.id)
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


# --- paths with spaces and non-ASCII names ---------------------------------------------------

def test_a_fix_job_works_in_a_path_with_spaces(tmp_path, run_job):
    test = ("import importlib.util, pathlib\n"
            "p = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'my calc.py'\n"
            "spec = importlib.util.spec_from_file_location('calc', p); m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n\n\ndef test_add():\n    assert m.add(1, 2) == 3\n")
    repo = make_repo(tmp_path / "My Name" / "my repo",
                     {"src/my calc.py": SOURCE.encode(), "tests/test_my_calc.py": test.encode()})
    job, steps, logs = run_job(repo, ".", {"src/my calc.py": DOCUMENTED})
    assert job.status == "done", "\n".join(logs) + json.dumps(steps, default=str)
    assert "src/my calc.py" in produced(job) and b"Return the sum" in out(job, "src/my calc.py")
    assert "the test command passed" in steps[1]["output"], steps[1]["output"]
    assert git(repo, "status", "--porcelain") == ""


def test_a_fix_job_on_a_non_ascii_file_name(tmp_path, run_job):
    repo = make_repo(tmp_path / "repo", {"src/café.py": SOURCE.encode(), "tests/test_ok.py": PASSING})
    job, steps, logs = run_job(repo, ".", {"src/café.py": DOCUMENTED})
    assert job.status == "done", "\n".join(logs)
    assert "kept src/café.py" in steps[1]["output"], steps[1]["output"]
    assert b"Return the sum" in out(job, "src/café.py")


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
    job, steps, logs = run_job(repo, "src/calc.py")
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
