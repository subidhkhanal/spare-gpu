"""A job's copy, and a review's listing, leave out what BharatCode Desktop would run or obey and the
files that hold credentials, even when git tracks them; .gtu/copy-exclude and .gtu/copy-include are
the project's own say; and key-shaped strings never reach the model."""

import json
import sys

import pytest

from gpu_token_utilization import jobs, redact, worktree
from gpu_token_utilization.fakeapi import Reply
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.scheduler import Scheduler

from conftest import git

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # key-shaped, not a key

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


# --- the review listing -----------------------------------------------------------------------

def listed_paths(listing):
    return {line.split(":", 1)[0] for line in listing.splitlines() if line}


def test_the_review_listing_skips_the_same_files(repo):
    listing, omitted = jobs.numbered_listing(repo, ".", 100_000)
    assert listed_paths(listing) == {"src/calc.py", "src/config.json", "docs/notes.md", "docs/drafts/plan.md"}
    assert omitted == []
    assert jobs.numbered_listing(repo, "opencode.json", 100_000) == ("", [])  # asked for by name: still not listed
    assert jobs.numbered_listing(repo, ".env", 100_000) == ("", [])


@pytest.fixture
def run_review(store, client, fake, desktop_fake):
    """A review job against the fakes; returns the job, its steps, the listing the model was sent, the log."""
    def run(repo, target):
        fake.queue(Reply(content="## Likely bugs\nNothing found.\n\n## Risky code\nNothing found.\n\n## Suggestions\nNothing found."))
        job = store.add_job("review", target, str(repo))
        logs = []
        Scheduler(store, client, agent=desktop_fake.agent(timeout=60),
                  config=JobConfig(test_command=(sys.executable, "-m", "pytest")),
                  sleep=fake.clock.sleep, now=fake.clock.utcnow, log=logs.append).run()
        sent = json.loads([r for r in fake.requests if r.url.path.endswith("/chat/completions")][-1].content)
        return store.get_job(job.id), store.steps(job.id), sent["messages"][1]["content"], logs

    return run


def test_the_listing_sent_for_review_has_keys_redacted(git_repo, run_review):
    (git_repo / "src" / "settings.py").write_text(
        f'AWS_KEY = "{AWS_KEY}"\nPASSWORD = "hunter2hunter2"\nNAME = "spare-gpu"\n', encoding="utf-8")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "settings")
    job, steps, prompt, logs = run_review(git_repo, "src")
    assert job.status == "done", "\n".join(logs)
    assert AWS_KEY not in prompt and "hunter2hunter2" not in prompt
    assert 'src/settings.py:1| AWS_KEY = "[redacted]"' in prompt and 'PASSWORD = "[redacted]"' in prompt
    assert 'NAME = "spare-gpu"' in prompt  # only the key-shaped parts went
    assert "Redacted 2 key-shaped string(s)" in steps[0]["output"]


# --- redact ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, label", [
    (f"key = {AWS_KEY}", "AWS access key"),
    ("OPENAI=sk-abcdefghijklmnop1234", "API token"),
    ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "API token"),
    ("github_pat_11ABCDEFG0123456789_abcdefghij", "API token"),
    ("xoxb-1234567890-ABCDEFGHIJ", "API token"),
    ("bc_live_abcdefghijklmnop", "API token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "JWT"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0\n-----END RSA PRIVATE KEY-----",
     "private key"),
    ("password=hunter2hunter2", "password or token value"),
    ("TOKEN: 'abc123def456'", "password or token value"),
    ('api_key = "AbCdEf123456"', "password or token value"),
    ("DB_PASSWD=correct-horse-9", "password or token value"),
])
def test_key_shaped_strings_are_found_and_redacted(text, label):
    assert [m.label for m in redact.find(text)] == [label]
    out, count = redact.redact(text)
    assert count == 1 and "[redacted]" in out


def test_only_the_value_of_a_password_goes_and_the_key_block_goes_whole():
    assert redact.redact("password = 'hunter2hunter2'  # dev") == ("password = '[redacted]'  # dev", 1)
    block = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n-----END PRIVATE KEY-----\n"
    assert redact.redact("key:\n" + block) == ("key:\n[redacted]\n", 1)


@pytest.mark.parametrize("text", [
    "auth(password=password)",
    "token = get_token()",
    "token = access_token",
    'label = "Password:"',
    "token = os.environ.get('T')",
    "the task-manager desk-lamp",
    "x = AKIA123",
    "KEY = 'bc_live_fake_key'",
    "if token == 'abcdefghij12':",
])
def test_ordinary_code_is_left_alone(text):
    assert redact.redact(text) == (text, 0)


def test_a_token_inside_a_token_value_counts_once():
    out, count = redact.redact("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and sk-abcdefghijklmnop")
    assert (out, count) == ("token=[redacted] and [redacted]", 2)
