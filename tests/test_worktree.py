"""Each job's private copy of the project: made from the files as they are (any folder, git or
not, saved or not), its own history for checking and resuming, and nothing left in the project
afterwards but the spare-gpu/<date>_<time>/ folder."""

import re
from datetime import datetime

import pytest

from gpu_token_utilization import worktree
from gpu_token_utilization.worktree import GitError

from conftest import git

WHEN = datetime(2026, 11, 5, 14, 32, 7)
CALC = "def add(a, b):\n    return a + b\n"


def project_state(repo):
    """Everything about the user's repo a job must leave alone."""
    return (git(repo, "rev-parse", "HEAD"), git(repo, "branch", "--list"), git(repo, "status", "--porcelain"),
            (repo / "src" / "calc.py").read_text())


@pytest.fixture
def plain_folder(tmp_path):
    """A project that isn't a git repo at all."""
    folder = tmp_path / "plain"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "calc.py").write_text(CALC)
    (folder / "node_modules" / "big").mkdir(parents=True)
    (folder / "node_modules" / "big" / "index.js").write_text("// huge")
    return folder


BUILD_OUTPUT = ("dist/bundle.js", "build/lib/a.py", "target/debug/bin", ".next/cache/c", ".gradle/caches/g",
                "out/o.txt", "coverage/lcov.info")


def with_build_output(folder):
    for name in BUILD_OUTPUT:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n")


# --- making the copy --------------------------------------------------------------------------

def test_the_copy_has_the_projects_files_and_its_own_history(git_repo):
    before = project_state(git_repo)
    path = worktree.create(git_repo, "abc123")
    assert path == git_repo / ".gtu" / "worktrees" / "abc123"
    assert (path / "src" / "calc.py").read_text() == CALC
    assert git(path, "rev-parse", "--show-toplevel").replace("\\", "/").lower() == str(path).replace("\\", "/").lower()
    assert git(path, "status", "--porcelain") == ""  # the starting point is saved
    assert project_state(git_repo) == before  # no branch, no commit, nothing changed in the user's repo


def test_a_folder_that_isnt_a_git_repo_works(plain_folder):
    path = worktree.create(plain_folder, "abc123")
    assert (path / "src" / "calc.py").read_text() == CALC
    assert not (path / "node_modules").exists()  # dependency folders aren't copied
    assert not (plain_folder / ".git").exists()  # the project doesn't become a repo
    assert worktree.repo_root(plain_folder) == plain_folder


def test_build_output_folders_are_not_copied_from_a_plain_folder(plain_folder):
    with_build_output(plain_folder)
    path = worktree.create(plain_folder, "abc123")
    assert (path / "src" / "calc.py").exists()
    assert not any((path / name).exists() for name in BUILD_OUTPUT)


def test_untracked_build_output_is_not_copied_from_a_git_repo_but_committed_files_always_are(git_repo):
    with_build_output(git_repo)  # untracked, and no .gitignore to hide it
    (git_repo / "build" / "setup.sh").write_text("# a build script the project commits\n")
    git(git_repo, "add", "build/setup.sh")
    git(git_repo, "commit", "-q", "-m", "build script")
    before = project_state(git_repo)
    path = worktree.create(git_repo, "abc123")
    assert (path / "build" / "setup.sh").exists()  # what the user committed is their project
    assert not (path / "build" / "lib").exists() and not any((path / n).exists() for n in BUILD_OUTPUT)
    assert project_state(git_repo) == before


def test_a_big_plain_folder_gets_a_note_and_is_still_copied(plain_folder, monkeypatch):
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 10)
    notes = []
    path = worktree.create(plain_folder, "abc123", warn=notes.append)
    assert len(notes) == 1 and "isn't a git repo" in notes[0] and re.search(r"\d+ [KM]B", notes[0])
    assert (path / "src" / "calc.py").exists()  # a note never stops the copy
    assert worktree.size_warning(plain_folder) == notes[0]


def test_a_small_plain_folder_and_any_git_repo_get_no_note(plain_folder, git_repo, monkeypatch):
    notes = []
    worktree.create(plain_folder, "abc123", warn=notes.append)
    assert notes == [] and worktree.size_warning(plain_folder) is None
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 1)
    worktree.create(git_repo, "abc123", warn=notes.append)
    assert notes == [] and worktree.size_warning(git_repo) is None  # its .gitignore does that job


def test_skipped_folders_dont_count_toward_the_note(plain_folder, monkeypatch):
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 1000)
    (plain_folder / "node_modules" / "big" / "blob").write_bytes(b"x" * 100_000)
    (plain_folder / "dist").mkdir()
    (plain_folder / "dist" / "bundle.js").write_bytes(b"x" * 50_000)
    assert worktree.size_warning(plain_folder) is None  # only what would be copied is measured


def test_resuming_a_copy_does_not_repeat_the_note(plain_folder, monkeypatch):
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 10)
    notes = []
    worktree.create(plain_folder, "abc123", warn=notes.append)
    worktree.create(plain_folder, "abc123", warn=notes.append)
    assert len(notes) == 1


def test_unsaved_changes_are_copied_as_they_are(git_repo):
    (git_repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a  # edited, not committed\n")
    (git_repo / "notes.txt").write_text("new, untracked\n")
    path = worktree.create(git_repo, "abc123")
    assert "edited, not committed" in (path / "src" / "calc.py").read_text()
    assert (path / "notes.txt").exists()


def test_files_the_repo_ignores_are_not_copied(git_repo):
    (git_repo / ".gitignore").write_text("secret.env\n")
    (git_repo / "secret.env").write_text("KEY=1\n")
    path = worktree.create(git_repo, "abc123")
    assert (path / ".gitignore").exists() and not (path / "secret.env").exists()


def test_line_endings_are_kept_exactly(plain_folder):
    (plain_folder / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    path = worktree.create(plain_folder, "abc123")
    (path / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    worktree.commit(path, "edit")
    folder = worktree.export(plain_folder, "abc123", None, WHEN)
    assert (folder / "win.txt").read_bytes() == b"one\r\ntwo\r\nthree\r\n"


def test_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(GitError, match="not a folder"):
        worktree.ensure_clean(tmp_path / "nowhere")


# --- saving steps -----------------------------------------------------------------------------

def test_excluded_files_are_never_saved(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "opencode.json").write_text("{}")
    (path / "notes.md").write_text("hi")
    worktree.commit(path, "work", exclude=("opencode.json",))
    assert git(path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["notes.md"]


def test_generated_caches_are_never_saved(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "src" / "__pycache__").mkdir()
    (path / "src" / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\0bytecode")
    (path / ".pytest_cache").mkdir()
    (path / ".pytest_cache" / "README.md").write_text("cache")
    (path / "real.py").write_text("x = 1\n")
    worktree.commit(path, "work")
    assert git(path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["real.py"]


def test_saving_with_no_changes_returns_none(git_repo):
    assert worktree.commit(worktree.create(git_repo, "abc123"), "nothing") is None


def test_create_again_resumes_the_same_copy(git_repo):
    first = worktree.create(git_repo, "abc123")
    (first / "partial.txt").write_text("unfinished")
    assert worktree.create(git_repo, "abc123") == first
    assert (first / "partial.txt").exists()


def test_a_copy_cut_short_is_made_again(git_repo):
    broken = worktree.worktree_path(git_repo, "abc123")
    broken.mkdir(parents=True)
    (broken / "half.txt").write_text("copy interrupted")  # no history yet: the copy never finished
    path = worktree.create(git_repo, "abc123")
    assert not (path / "half.txt").exists() and (path / "src" / "calc.py").exists()


def test_discard_uncommitted_drops_partial_edits(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "src" / "calc.py").write_text("half-written")
    (path / "new.py").write_text("half-written")
    worktree.discard_uncommitted(path)
    assert (path / "src" / "calc.py").read_text() == CALC
    assert not (path / "new.py").exists()
    assert (git_repo / "src" / "calc.py").read_text() == CALC


def test_remove_leaves_nothing_behind(git_repo):
    before = project_state(git_repo)
    path = worktree.create(git_repo, "abc123")
    (path / "done.txt").write_text("result")
    worktree.commit(path, "result")
    worktree.remove(git_repo, "abc123")
    assert not path.exists()
    assert project_state(git_repo) == before  # no gtu/ branch, nothing


def test_remove_is_safe_when_nothing_exists(git_repo):
    worktree.remove(git_repo, "never-created")


# --- the spare-gpu/<date>_<time>/ folder ------------------------------------------------------

def finished_job(repo, job_id="abc123"):
    """A job that edited src/calc.py and added tests/test_calc.py."""
    path = worktree.create(repo, job_id)
    (path / "src" / "calc.py").write_text('def add(a, b):\n    """Add two numbers."""\n    return a + b\n')
    (path / "tests").mkdir()
    (path / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    worktree.commit(path, "job work")
    return path


def test_a_finished_jobs_edited_files_are_copied_to_a_dated_folder(git_repo):
    finished_job(git_repo)
    folder = worktree.export(git_repo, "abc123", None, WHEN)
    assert folder == git_repo / "spare-gpu" / "2026-11-05_14-32-07"
    copied = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert copied == ["src/calc.py", "tests/test_calc.py"]  # only what the job changed, paths kept
    assert '"""Add two numbers."""' in (folder / "src" / "calc.py").read_text()
    assert (git_repo / "src" / "calc.py").read_text() == CALC  # the user's own file is untouched


def test_it_works_the_same_in_a_plain_folder(plain_folder):
    finished_job(plain_folder)
    folder = worktree.export(plain_folder, "abc123", None, WHEN)
    worktree.remove(plain_folder, "abc123")
    assert (folder / "src" / "calc.py").exists() and (folder / "tests" / "test_calc.py").exists()
    assert sorted(p.name for p in plain_folder.iterdir()) == [".gtu", "node_modules", "spare-gpu", "src"]


def test_the_folder_never_makes_the_repo_dirty(git_repo):
    finished_job(git_repo)
    worktree.export(git_repo, "abc123", None, WHEN)
    worktree.remove(git_repo, "abc123")
    assert git(git_repo, "status", "--porcelain") == ""


def test_two_jobs_finishing_in_the_same_second_get_their_own_folders(git_repo):
    finished_job(git_repo, "abc123")
    finished_job(git_repo, "def456")
    first = worktree.export(git_repo, "abc123", None, WHEN)
    second = worktree.export(git_repo, "def456", None, WHEN)
    assert first.name == "2026-11-05_14-32-07" and second.name == "2026-11-05_14-32-07-2"


def test_a_job_that_changed_nothing_gets_no_folder(git_repo):
    worktree.create(git_repo, "abc123")
    assert worktree.export(git_repo, "abc123", None, WHEN) is None
    assert not (git_repo / "spare-gpu").exists()
