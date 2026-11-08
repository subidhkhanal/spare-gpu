"""The git hooks, against real repos: `gtu hooks install` writes ours and leaves yours alone,
pre-commit stops a commit that adds a key or touches Desktop's tool folders, post-commit queues a
review of the commit, and a review job on a commit target reads `git show` through the job's copy."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import gpu_token_utilization
from gpu_token_utilization import actions, hooks, jobs
from gpu_token_utilization.cli import app
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler
from gpu_token_utilization.store import Store

from conftest import git

runner = CliRunner()
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # key-shaped, not a key
IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
WITH_SUB = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A committed repo, as the current folder, whose hooks run this very package: a hook calls
    Python by path, and that Python must find gpu_token_utilization where this test does."""
    monkeypatch.delenv("GTU_DB", raising=False)
    src = str(Path(gpu_token_utilization.__file__).resolve().parents[1])
    had = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + had if had else src)
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    monkeypatch.chdir(root)
    return root


def commit(repo, message, *flags):
    """`git commit`, hooks and all; returns the finished process rather than raising."""
    return subprocess.run(["git", *IDENTITY, "commit", "-q", "-m", message, *flags], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def hook(repo, name):
    return repo / ".git" / "hooks" / name


# --- install and uninstall --------------------------------------------------------------------

def test_install_writes_both_hooks_as_sh_scripts_calling_this_python(repo):
    lines = hooks.install(repo)
    for name in hooks.HOOKS:
        text = hook(repo, name).read_bytes()
        assert text.startswith(b"#!/bin/sh\n") and b"\r" not in text  # git's sh on Windows chokes on CRLF
        assert hooks.MARKER.encode() in text
        assert hooks.sh_python().encode() in text  # this Python, by absolute path, quoted
        if os.name != "nt":
            assert os.access(hook(repo, name), os.X_OK)
    assert b"-m gpu_token_utilization guard" in hook(repo, "pre-commit").read_bytes()
    assert b"-m gpu_token_utilization add review --commit HEAD" in hook(repo, "post-commit").read_bytes()
    assert sum("Wrote" in line for line in lines) == 2 and "--no-verify" in lines[-1]
    again = hooks.install(repo)  # running it again only refreshes ours
    assert sum("Updated" in line for line in again) == 2


def test_a_hook_of_your_own_is_not_touched_and_the_line_to_add_is_printed(repo):
    mine = hook(repo, "pre-commit")
    mine.parent.mkdir(exist_ok=True)
    mine.write_text("#!/bin/sh\necho my own hook\n", encoding="utf-8")
    said = "\n".join(hooks.install(repo))
    assert mine.read_text(encoding="utf-8") == "#!/bin/sh\necho my own hook\n"
    assert "isn't spare-gpu's" in said and "-m gpu_token_utilization guard || exit 1" in said
    assert hooks.is_ours(hook(repo, "post-commit"))  # the other one was still written


def test_uninstall_removes_only_our_hooks(repo):
    hooks.install(repo)
    hook(repo, "post-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")  # replaced ours with your own
    lines = hooks.uninstall(repo)
    assert not hook(repo, "pre-commit").exists()
    assert hook(repo, "post-commit").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert any("Removed" in line for line in lines) and any("isn't spare-gpu's" in line for line in lines)
    hook(repo, "post-commit").unlink()
    assert hooks.uninstall(repo) == ["No spare-gpu hooks were installed here; nothing changed."]


def test_a_repo_whose_hooks_live_elsewhere_is_left_alone(repo):
    git(repo, "config", "core.hooksPath", ".husky")
    with pytest.raises(hooks.HookError, match="core.hooksPath") as exc:
        hooks.install(repo)
    assert "-m gpu_token_utilization guard" in str(exc.value)
    assert not (repo / ".husky").exists() and not hook(repo, "pre-commit").exists()


def test_outside_a_repo_there_is_nothing_to_install(tmp_path):
    with pytest.raises(hooks.HookError, match="not in a git repo"):
        hooks.install(tmp_path)


# --- pre-commit -------------------------------------------------------------------------------

def test_a_commit_adding_a_key_shaped_string_is_stopped(repo):
    hooks.install(repo)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "settings.py").write_text(f"NAME = 'x'\nAWS_KEY = '{AWS_KEY}'\n", encoding="utf-8")
    git(repo, "add", "-A")
    result = commit(repo, "add settings")
    assert result.returncode != 0
    assert "src/settings.py:2" in result.stderr and "AWS access key" in result.stderr
    assert "git commit --no-verify" in result.stderr
    assert AWS_KEY not in result.stderr  # the message doesn't repeat it
    assert git(repo, "rev-parse", "HEAD") == head  # nothing was committed
    skipped = commit(repo, "add settings anyway", "--no-verify")
    assert skipped.returncode == 0, skipped.stderr
    assert git(repo, "rev-parse", "HEAD") != head


def test_a_commit_adding_a_desktop_tool_is_stopped(repo):
    hooks.install(repo)
    tool = repo / ".opencode" / "tool" / "x.ts"
    tool.parent.mkdir(parents=True)
    tool.write_text("export default {}\n", encoding="utf-8")
    git(repo, "add", "-A")
    result = commit(repo, "add a tool")
    assert result.returncode != 0
    assert ".opencode/tool/x.ts" in result.stderr and "BharatCode Desktop" in result.stderr
    assert "git commit --no-verify" in result.stderr


def test_guard_reads_the_staged_changes_as_they_will_be_committed(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (repo / "src" / "conf.py").write_bytes(b"x = 1\r\npassword = 'hunter2hunter2'\r\n")  # CRLF: still found, right line
    (repo / "notes.txt").write_text(f"unstaged {AWS_KEY}\n", encoding="utf-8")  # not staged: not the commit's business
    git(repo, "add", ".claude", "src")
    findings = hooks.guard(repo)
    assert {(f.path, f.line) for f in findings} == {(".claude/settings.json", None), ("src/conf.py", 2)}
    text = hooks.explain(findings)
    assert "src/conf.py:2" in text and ".claude/settings.json" in text and "git commit --no-verify" in text
    git(repo, "commit", "-q", "--no-verify", "-m", "with")
    git(repo, "rm", "-q", "--cached", ".claude/settings.json")  # removing a guarded file is fine
    assert hooks.guard(repo) == []


# --- post-commit ------------------------------------------------------------------------------

def test_a_normal_commit_passes_and_queues_a_review_of_it(repo):
    hooks.install(repo)
    (repo / "src" / "calc.py").write_text(WITH_SUB, encoding="utf-8")
    git(repo, "add", "-A")
    result = commit(repo, "add sub")
    assert result.returncode == 0, result.stderr
    said = result.stdout + result.stderr
    assert said.count("\n") == 1 and "gtu start" in said and "spare-gpu: queued a review of commit" in said
    sha = git(repo, "rev-parse", "HEAD")
    with Store(repo / ".gtu" / "gtu.db") as store:
        (job,) = store.list_jobs()
    assert (job.kind, job.target, job.status) == ("review", f"commit:{sha}", "queued")
    assert git(repo, "status", "--porcelain") == ""  # .gtu ignores itself; nothing ran


# --- the commit target ------------------------------------------------------------------------

def test_queue_job_resolves_the_commit_and_refuses_what_it_cant(repo):
    sha = git(repo, "rev-parse", "HEAD")

    def open_store(root):
        return Store(root / ".gtu" / "gtu.db")

    job = actions.queue_job(open_store, repo, "review", ".", commit="HEAD").job
    assert job.target == f"commit:{sha}" and Path(job.repo) == repo.resolve()
    also = actions.queue_job(open_store, repo, "review", f"commit:{sha[:7]}").job  # in the target, as a tool would pass it
    assert also.target == f"commit:{sha}"
    with pytest.raises(actions.ActionError, match="only a review job"):
        actions.queue_job(open_store, repo, "tests", ".", commit="HEAD")
    with pytest.raises(actions.ActionError, match="no commit 'nope'"):
        actions.queue_job(open_store, repo, "review", ".", commit="nope")


def test_the_commands_from_the_terminal(repo):
    installed = runner.invoke(app, ["hooks", "install"])
    assert installed.exit_code == 0 and "Wrote" in installed.output and "--uninstall" in installed.output
    (repo / "src" / "k.py").write_text(f"k = '{AWS_KEY}'\n", encoding="utf-8")
    git(repo, "add", "-A")
    guarded = runner.invoke(app, ["guard"])
    assert guarded.exit_code == 1 and "src/k.py:1" in guarded.output and "--no-verify" in guarded.output
    git(repo, "reset", "-q")
    assert runner.invoke(app, ["guard"]).exit_code == 0
    sha = git(repo, "rev-parse", "HEAD")
    queued = runner.invoke(app, ["add", "review", "--commit", "HEAD"])
    assert queued.exit_code == 0 and f"review commit:{sha}" in queued.output
    quiet = runner.invoke(app, ["add", "review", "--commit", "HEAD", "--quiet"])
    assert quiet.exit_code == 0 and quiet.output.count("\n") == 1 and "gtu start" in quiet.output
    assert runner.invoke(app, ["add", "review"]).exit_code == 1
    assert runner.invoke(app, ["add", "review", "src", "--commit", "HEAD"]).exit_code == 1
    assert runner.invoke(app, ["add", "tests", "--commit", "HEAD"]).exit_code == 1
    missing = runner.invoke(app, ["add", "review", "--commit", "nope"])
    assert missing.exit_code == 1 and "no commit 'nope'" in missing.output
    dry = runner.invoke(app, ["start", "--dry-run"])  # the dry run reads the commit from the repo itself
    assert dry.exit_code == 0 and f"commit:{sha}" in dry.output, dry.output
    removed = runner.invoke(app, ["hooks", "install", "--uninstall"])
    assert removed.exit_code == 0 and "Removed" in removed.output


# --- a review job on a commit -----------------------------------------------------------------

@pytest.fixture
def run_review(store, client, fake, desktop_fake):
    """A review job against the fakes; returns the job, its steps, the listing the model was sent, the log."""
    def run(repo, target):
        fake.queue(Reply(content="## Likely bugs\n- `src/calc.py` sub has no type checks\n\n## Risky code\n"
                                 "Nothing found.\n\n## Suggestions\nNothing found."))
        job = store.add_job("review", target, str(repo))
        logs = []
        Scheduler(store, client, agent=desktop_fake.agent(timeout=60),
                  config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                  sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
        sent = json.loads([r for r in fake.requests if r.url.path.endswith("/chat/completions")][-1].content)
        return store.get_job(job.id), store.steps(job.id), sent["messages"][1]["content"], logs

    return run


def test_a_review_of_a_commit_reads_git_show_through_the_jobs_copy(repo, run_review):
    (repo / "src" / "calc.py").write_text(WITH_SUB, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add sub")
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("# later\n", encoding="utf-8")  # a later commit: not what is reviewed
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later")
    head = git(repo, "rev-parse", "HEAD")

    job, steps, prompt, logs = run_review(repo, f"commit:{sha}")
    assert job.status == "done", "\n".join(logs)
    assert "add sub" in prompt and "+def sub(a, b):" in prompt and "src/calc.py |" in prompt  # message, diff, stat
    assert "# later" not in prompt and "README.md" not in prompt
    assert "git show" in prompt and "path:line" not in prompt  # the ask fits a diff, not a numbered listing
    report = (repo / job.output_dir / f"gtu-review-{job.id}.md").read_text(encoding="utf-8")
    assert report.startswith(f"# Code review: `commit {sha[:12]}`") and "sub has no type checks" in report
    assert "Redacted 0 key-shaped string(s)" in steps[0]["output"]
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--list") == "* main"  # the fetch into the copy left nothing in the user's repo


def test_a_big_commit_is_cut_at_the_size_limit(repo):
    (repo / "big.py").write_text("".join(f"x{n} = {n}\n" for n in range(1, 501)), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "big")
    sha = git(repo, "rev-parse", "HEAD")
    listing, omitted = jobs.numbered_listing(repo, f"commit:{sha}", 1_500)
    assert len(listing) <= 1_500 and listing.endswith("\n") and listing.startswith(f"commit {sha}")
    assert omitted == [f"commit {sha[:12]} (cut after line {listing.count(chr(10))})"]
    whole, nothing_left_out = jobs.numbered_listing(repo, f"commit:{sha}", 1_000_000)
    assert nothing_left_out == [] and "+x500 = 500" in whole
