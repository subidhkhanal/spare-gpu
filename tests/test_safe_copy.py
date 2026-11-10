"""A job's copy leaves out what BharatCode Desktop would run or obey and the files that hold
credentials, even when git tracks them; .gtu/copy-exclude and .gtu/copy-include are the project's
own say."""

import pytest

from gpu_token_utilization import worktree

from conftest import git

TRACKED = {
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "src/config.json": '{"theme": "dark"}\n',  # not at the root: an ordinary data file
    ".opencode/tool/x.ts": "export default async () => {}\n",  # Desktop would run it
    ".opencode/agent/helper.md": "# an agent\n",
    ".claude/settings.json": "{}\n",
    ".env": "SECRET=1\n",
    ".env.example": "SECRET=\n",
    "opencode.json": '{"permission": {"bash": "allow"}}\n',
    "config.json": "{}\n",
    "certs/server.key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
    "keys/id_rsa.pub": "ssh-rsa AAAA\n",
    ".github/workflows/ci.yml": "on: push\n",
    "docs/notes.md": "# notes\n",
    "docs/drafts/plan.md": "# plan\n",
}
EXCLUDED = (".opencode/tool/x.ts", ".opencode/agent/helper.md", ".claude/settings.json", ".env", ".env.example",
            "opencode.json", "config.json", "certs/server.key", "keys/id_rsa.pub")


@pytest.fixture
def repo(tmp_path):
    """A committed repo that tracks Desktop's folders and config, credential files and a CI workflow."""
    root = tmp_path / "project"
    for name, text in TRACKED.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def copied(root, job_id="abc123"):
    path = worktree.create(root, job_id)
    return {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and ".git" not in p.parts}


# --- the copy ---------------------------------------------------------------------------------

def test_desktops_folders_and_credential_files_are_not_copied_even_when_tracked(repo):
    files = copied(repo)
    assert {"src/calc.py", "src/config.json", ".github/workflows/ci.yml", "docs/notes.md"} <= files
    for name in EXCLUDED:
        assert name not in files, name
    assert files == set(worktree.project_files(repo))
    assert git(repo, "status", "--porcelain") == ""  # the user's repo is as it was


def test_the_same_in_a_folder_that_isnt_a_repo_whatever_the_case(tmp_path):
    root = tmp_path / "plain"
    for name in ("src/a.py", ".OpenCode/Tool/y.ts", ".env", "Secrets.PEM", "opencode.json", "sub/opencode.json"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n", encoding="utf-8")
    assert set(worktree.project_files(root)) == {"src/a.py", "sub/opencode.json"}  # Desktop's config counts at the root only


def test_copy_include_brings_a_file_back_but_never_desktops_tools(repo):
    (repo / ".gtu").mkdir()
    (repo / ".gtu" / "copy-include").write_text("# what the copy may have after all\n.env.example\n.opencode/tool/*\n",
                                                encoding="utf-8")
    files = copied(repo)
    assert ".env.example" in files and ".env" not in files
    assert ".opencode/tool/x.ts" not in files
    assert git(repo, "status", "--porcelain") == ""  # .gtu ignores itself, its lists included


def test_copy_exclude_leaves_a_folder_out(repo):
    (repo / ".gtu").mkdir()
    (repo / ".gtu" / "copy-exclude").write_text("docs\\drafts\\*\n", encoding="utf-8")  # written on Windows, with backslashes
    files = copied(repo)
    assert "docs/notes.md" in files and "docs/drafts/plan.md" not in files


def test_copy_include_overrides_copy_exclude_too(repo):
    (repo / ".gtu").mkdir()
    (repo / ".gtu" / "copy-exclude").write_text("docs/*\n", encoding="utf-8")
    (repo / ".gtu" / "copy-include").write_text("docs/notes.md\n", encoding="utf-8")
    files = copied(repo)
    assert "docs/notes.md" in files and "docs/drafts/plan.md" not in files
