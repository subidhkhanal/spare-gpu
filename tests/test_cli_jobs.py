import re

import pytest
from typer.testing import CliRunner

from gpu_token_utilization.cli import app

from conftest import git

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def test_add_notes_when_a_plain_folder_is_big(tmp_path, monkeypatch):
    from gpu_token_utilization import worktree

    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(worktree, "WARN_COPY_BYTES", 1)
    result = runner.invoke(app, ["add", "review", "src"])
    assert result.exit_code == 0 and "Queued job" in result.output
    assert "Note:" in result.output and "isn't a git repo" in result.output


def test_add_works_with_unsaved_changes(repo):
    # each job copies the files as they are, so nothing needs committing first
    (repo / "src" / "a.py").write_text("changed\n")
    result = runner.invoke(app, ["add", "tests", "src/a.py"])
    assert result.exit_code == 0, result.output


def test_add_works_in_a_folder_that_is_not_a_git_repo(tmp_path_factory, monkeypatch):
    plain = tmp_path_factory.mktemp("plain")
    (plain / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(plain)
    result = runner.invoke(app, ["add", "tests", "a.py"])
    assert result.exit_code == 0, result.output
    assert not (plain / ".git").exists()  # the folder doesn't become a repo


def test_queueing_a_job_leaves_the_repo_clean(repo):
    add("tests", "src/a.py")
    assert git(repo, "status", "--porcelain") == ""  # .gtu/ ignores itself


def add(*args):
    result = runner.invoke(app, ["add", *args])
    assert result.exit_code == 0, result.output
    return re.search(r"Queued job (\w+)", result.output).group(1)


def test_add_list_status_cancel(repo):
    job_id = add("tests", "src/a.py", "--note", "edge cases")
    assert (repo / ".gtu" / "gtu.db").exists()

    listed = runner.invoke(app, ["list"])
    assert job_id in listed.output and "queued" in listed.output and "src/a.py" in listed.output

    shown = runner.invoke(app, ["status", job_id])
    assert shown.exit_code == 0
    assert "status:   queued" in shown.output and "edge cases" in shown.output
    assert "requests: 0" in shown.output

    cancelled = runner.invoke(app, ["cancel", job_id])
    assert cancelled.exit_code == 0 and "Cancelled" in cancelled.output
    assert job_id not in runner.invoke(app, ["list", "--active"]).output

    again = runner.invoke(app, ["cancel", job_id])
    assert again.exit_code == 1 and "already cancelled" in again.output


def test_add_rejects_missing_path(repo):
    result = runner.invoke(app, ["add", "docs", "nope.py"])
    assert result.exit_code == 1 and "does not exist" in result.output


def test_add_rejects_path_outside_repo(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.py"
    outside.write_text("")
    result = runner.invoke(app, ["add", "review", str(outside)])
    assert result.exit_code == 1 and "outside this repo" in result.output


def test_add_rejects_unknown_kind(repo):
    result = runner.invoke(app, ["add", "bulk", "src/a.py"])
    assert result.exit_code != 0


def test_status_of_unknown_job_fails(repo):
    result = runner.invoke(app, ["status", "deadbeef"])
    assert result.exit_code == 1 and "no job" in result.output


def test_list_when_empty(repo):
    assert "No jobs." in runner.invoke(app, ["list"]).output


def test_start_without_key_fails(repo, monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1 and "BHARATCODE_API_KEY" in result.output
