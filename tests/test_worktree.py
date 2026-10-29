import pytest

from gpu_token_utilization import worktree
from gpu_token_utilization.worktree import GitError

from conftest import git


def main_state(repo):
    return git(repo, "rev-parse", "HEAD"), git(repo, "branch", "--show-current"), git(repo, "status", "--porcelain")


def test_creates_branch_and_worktree_without_touching_main(git_repo):
    before = main_state(git_repo)
    path = worktree.create(git_repo, "abc123")
    assert path == git_repo / ".gtu" / "worktrees" / "abc123"
    assert git(path, "branch", "--show-current") == "gtu/abc123"
    assert git(path, "rev-parse", "HEAD") == before[0]  # starts from the user's HEAD
    assert main_state(git_repo) == before  # same commit, same branch, still clean


def test_commits_land_on_the_job_branch_only(git_repo):
    main_head = git(git_repo, "rev-parse", "main")
    path = worktree.create(git_repo, "abc123")
    (path / "tests").mkdir()
    (path / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    commit_id = worktree.commit(path, "add tests")
    assert commit_id and git(git_repo, "rev-parse", "gtu/abc123") == commit_id
    assert git(git_repo, "rev-parse", "main") == main_head
    assert not (git_repo / "tests").exists()
    assert git(git_repo, "status", "--porcelain") == ""


def test_excluded_files_are_never_committed(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "opencode.json").write_text("{}")
    (path / "notes.md").write_text("hi")
    worktree.commit(path, "work", exclude=("opencode.json",))
    committed = git(path, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["notes.md"]


def test_commit_with_no_changes_returns_none(git_repo):
    path = worktree.create(git_repo, "abc123")
    assert worktree.commit(path, "nothing") is None


def test_job_commits_are_attributed_to_the_tool(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "x.txt").write_text("x")
    worktree.commit(path, "work")
    assert git(path, "log", "-1", "--format=%an <%ae>") == "spare-gpu <spare-gpu@localhost>"


@pytest.mark.parametrize("make_dirty", [
    lambda repo: (repo / "src" / "calc.py").write_text("changed\n"),
    lambda repo: (repo / "untracked.txt").write_text("new\n"),
])
def test_dirty_repo_is_refused(git_repo, make_dirty):
    make_dirty(git_repo)
    with pytest.raises(GitError, match="uncommitted changes"):
        worktree.create(git_repo, "abc123")
    assert git(git_repo, "branch", "--list", "gtu/*") == ""


def test_not_a_git_repo_is_refused(tmp_path):
    with pytest.raises(GitError, match="not inside a git repository"):
        worktree.ensure_clean(tmp_path)


def test_create_again_resumes_the_same_worktree(git_repo):
    first = worktree.create(git_repo, "abc123")
    (first / "partial.txt").write_text("unfinished")
    # The repo itself is still clean (the worktree folder is ignored), so resuming is allowed.
    assert worktree.create(git_repo, "abc123") == first
    assert (first / "partial.txt").exists()


def test_discard_uncommitted_drops_partial_edits(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "src" / "calc.py").write_text("half-written")
    (path / "new.py").write_text("half-written")
    worktree.discard_uncommitted(path)
    assert (path / "src" / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (path / "new.py").exists()
    assert (git_repo / "src" / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_remove_deletes_the_worktree_but_keeps_the_branch(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "done.txt").write_text("result")
    commit_id = worktree.commit(path, "result")
    (path / "leftover.txt").write_text("not committed")
    worktree.remove(git_repo, "abc123")
    assert not path.exists()
    assert "abc123" not in git(git_repo, "worktree", "list")
    assert git(git_repo, "rev-parse", "gtu/abc123") == commit_id


def test_recreate_after_worktree_was_removed_keeps_branch_history(git_repo):
    path = worktree.create(git_repo, "abc123")
    (path / "a.txt").write_text("a")
    commit_id = worktree.commit(path, "a")
    worktree.remove(git_repo, "abc123")
    again = worktree.create(git_repo, "abc123")
    assert git(again, "rev-parse", "HEAD") == commit_id


def test_remove_is_safe_when_nothing_exists(git_repo):
    worktree.remove(git_repo, "never-created")
