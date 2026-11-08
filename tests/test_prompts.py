"""`gtu prompts`: allow rules for the read-only commands the user keeps approving in Desktop,
read from a private copy of Desktop's database. The fixture database is built with the tables
Desktop's drizzle schema defines (packages/core/src/session/sql.ts, permission/sql.ts,
project/sql.ts), with the columns spare-gpu relies on, and tool calls stored the way Desktop
stores them: the V1 part JSON in `part`, the V2 assistant content in `session_message`."""

import hashlib
import inspect
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import cli, prompts
from gpu_token_utilization.cli import app

runner = CliRunner()
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

# The tables as sql.ts defines them (drizzle `text`/`integer`, Timestamps = time_created, time_updated in ms).
SCHEMA = """
CREATE TABLE project (
    id TEXT PRIMARY KEY, worktree TEXT NOT NULL, vcs TEXT, name TEXT, icon_url TEXT, icon_url_override TEXT,
    icon_color TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, time_initialized INTEGER,
    sandboxes TEXT NOT NULL, commands TEXT
);
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE, workspace_id TEXT,
    parent_id TEXT, slug TEXT NOT NULL, directory TEXT NOT NULL, path TEXT, title TEXT NOT NULL, version TEXT NOT NULL,
    share_url TEXT, summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER, summary_diffs TEXT,
    metadata TEXT, cost REAL NOT NULL DEFAULT 0, tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0, tokens_reasoning INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read INTEGER NOT NULL DEFAULT 0, tokens_cache_write INTEGER NOT NULL DEFAULT 0, revert TEXT,
    permission TEXT, goal TEXT, agent TEXT, model TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
    time_compacting INTEGER, time_archived INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE, type TEXT NOT NULL,
    seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE UNIQUE INDEX session_message_session_seq_idx ON session_message (session_id, seq);
CREATE TABLE permission (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    action TEXT NOT NULL, resource TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
);
CREATE UNIQUE INDEX permission_project_action_resource_idx ON permission (project_id, action, resource);
"""


class FakeDesktop:
    """Writes a bharatcode.db the way Desktop's projector does."""

    def __init__(self, path: Path, now: datetime = NOW):
        self.path, self.now, self.n = path, now, 0
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.project("p1")

    def _ms(self, days_ago: float) -> int:
        return int((self.now - timedelta(days=days_ago)).timestamp() * 1000)

    def project(self, project_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO project (id, worktree, time_created, time_updated, sandboxes) "
                          "VALUES (?, ?, ?, ?, '[]')", (project_id, f"C:\\work\\{project_id}", self._ms(60), self._ms(0)))
        self.conn.commit()

    def session(self, session_id: str, project_id: str = "p1") -> None:
        self.project(project_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO session (id, project_id, slug, directory, title, version, time_created, time_updated) "
            "VALUES (?, ?, ?, ?, ?, '1.0.0', ?, ?)",
            (session_id, project_id, session_id, f"C:\\work\\{project_id}", f"Session {session_id}", self._ms(30), self._ms(0)))
        self.conn.commit()

    def bash(self, session_id: str, command: str, days_ago: float = 1.0, status: str = "completed",
             project_id: str = "p1", call_id: str | None = None, v2: bool = False, tool: str = "bash") -> str:
        """One tool call, as a V1 part (default) or a V2 assistant message."""
        self.session(session_id, project_id)
        self.n += 1
        call_id = call_id or f"call_{self.n}"
        at = self._ms(days_ago)
        state = {"status": status, "input": {"command": command, "timeout": 120000}, "time": {"start": at - 500, "end": at}}
        if status == "completed":
            state |= {"output": "ok", "title": command[:40], "metadata": {}}
        else:
            state |= {"error": "exit code 1"}
        if not v2:
            message_id = f"msg_{self.n}"
            self.conn.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
                              (message_id, session_id, at - 1000, at, json.dumps({"role": "assistant", "time": {"created": at - 1000}})))
            part = {"type": "tool", "callID": call_id, "tool": tool, "state": state}
            self.conn.execute("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
                              (f"prt_{self.n}", message_id, session_id, at - 500, at, json.dumps(part)))
        else:
            content = [{"type": "text", "id": f"txt_{self.n}", "text": "Running it."},
                       {"type": "tool", "id": call_id, "name": tool, "state": state | {"content": [], "structured": {}},
                        "time": {"created": at - 500, "completed": at}}]
            data = {"agent": "build", "model": {"providerID": "bharatcode", "modelID": "deepseek-v4.1-flash"},
                    "content": content, "time": {"created": at - 1000, "completed": at}}
            self.conn.execute("INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) "
                              "VALUES (?, ?, 'assistant', ?, ?, ?, ?)", (f"smsg_{self.n}", session_id, self.n, at - 1000, at, json.dumps(data)))
        self.conn.commit()
        return call_id

    def saved(self, resource: str, project_id: str = "p1", action: str = "bash") -> None:
        """A rule Desktop saved when the user answered "always" (core/src/permission.ts)."""
        self.project(project_id)
        self.n += 1
        self.conn.execute("INSERT INTO permission (id, project_id, action, resource, time_created, time_updated) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (f"perm_{self.n}", project_id, action, resource, self._ms(5), self._ms(5)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def desktop(tmp_path):
    db = FakeDesktop(tmp_path / "Data" / "bharatcode.db") if (tmp_path / "Data").mkdir() is None else None
    yield db
    db.close()


def populate(db: FakeDesktop) -> None:
    """A month of approvals: some read-only, some not, some old, some failed."""
    for i, (session, command, days) in enumerate([
        ("s1", "git status", 1), ("s2", "git status --short", 2), ("s3", "git status -sb", 3),
        ("s4", r'"C:\Program Files\Git\bin\git.exe" status', 0.5), ("s1", "git status", 10),
    ]):
        db.bash(session, command, days)
    db.bash("s1", "python -m pytest tests -q", 1)
    db.bash("s2", r"C:\Python311\python.exe -m pytest", 2)
    db.bash("s3", "python -m pytest tests/test_x.py", 4)
    db.bash("s1", "ls -la", 1)  # only two sessions
    db.bash("s2", "ls", 1)
    for s in ("s1", "s2", "s3", "s4", "s5"):
        db.bash(s, "npm install", 1)  # writes: never proposed
    for s in ("s1", "s2", "s3"):
        db.bash(s, "git log --oneline -5", 2)  # read-only, but a saved rule already allows it
        db.bash(s, "git diff HEAD~1", 40)  # read-only, but outside the window
        db.bash(s, "cat README.md", 1, status="error")  # failed: not an approval that ran
        db.bash(s, "cd src && ls", 1)  # chained: the prefix doesn't describe it
        db.bash(s, "src/main.py", 1, tool="read")  # another tool entirely
    db.saved("git log *")


# --- commands to prefixes ---------------------------------------------------------------------------

@pytest.mark.parametrize("command, prefix, read_only", [
    ("git status --short", "git status", True),
    (r"C:\Python311\python.exe -m pytest tests", "python -m pytest", True),
    (r'"C:\Program Files\Git\bin\git.exe" log -5', "git log", True),
    ("npm run test -- --watch", "npm run test", True),
    ("npm run build", "npm run build", False),
    ("py -3.11 -m pytest", "py -m pytest", True),
    ("ls -la", "ls", True),
    ("git --no-pager diff HEAD~1", "git diff", True),
    ("git -C src status", "git status", True),
    ("& 'C:\\py\\python.exe' --version", "python --version", True),
    ("FOO=1 git status", "git status", True),
    ("pytest -q", "pytest", True),
    ("python script.py", "python script.py", False),
    ("rm -rf build", "rm", False),
    ("git push origin main", "git push", False),
    ("pip install requests", "pip install", False),
])
def test_commands_normalise_to_a_prefix(command, prefix, read_only):
    norm = prompts.normalise(command)
    assert norm.prefix == prefix and norm.read_only is read_only and not norm.compound


@pytest.mark.parametrize("command", [
    "cd src && ls", "echo hi > out.txt", "cat a.txt | grep x", "git status; rm -rf build", "ls `pwd`", "ls $(pwd)",
])
def test_chained_or_redirected_commands_are_never_read_only(command):
    norm = prompts.normalise(command)
    assert norm.compound and not norm.read_only


@pytest.mark.parametrize("command, read_only", [
    ("git branch", True), ("git branch -a", True), ("git branch --list 'feat*'", True), ("git branch --show-current", True),
    ("git branch -D main", False), ("git branch -m old new", False), ("git branch feature", False),
    ("find . -name '*.py'", True), ("find . -name '*.pyc' -delete", False), ("find . -exec rm {} ;", False),
    ("git diff --output=x.patch", False), ("git log --output x", False), ("tail -n 20 log.txt", True),
])
def test_a_flag_that_writes_takes_a_command_off_the_read_only_list(command, read_only):
    assert prompts.normalise(command).read_only is read_only


def test_the_read_only_list_has_nothing_that_writes():
    words = {w for p in prompts.READ_ONLY for w in p.split()}
    assert not words & {"install", "push", "rm", "del", "mv", "cp", "commit", "checkout", "reset", "merge", "rebase",
                        "add", "write", "uninstall", "publish", "deploy", "run"} - {"run"}  # `npm run test` is the one `run`
    assert all(p.split()[-1] == "test" for p in prompts.READ_ONLY if "run" in p.split())
    assert prompts.normalise("") is None


# --- Desktop's matching, ported ---------------------------------------------------------------------

def test_wildcard_match_follows_desktops_rules():
    assert prompts.wildcard_match("git status", "git status *", ignore_case=False)  # the trailing " *" is optional
    assert prompts.wildcard_match("git status --short", "git status *", ignore_case=False)
    assert not prompts.wildcard_match("git status --short", "git status", ignore_case=False)  # an exact rule is exact
    assert prompts.wildcard_match("anything at all", "*", ignore_case=False)
    assert prompts.wildcard_match("ls C:\\work", "ls C:/work*", ignore_case=False)  # slashes are normalised
    assert prompts.wildcard_match("Git Status", "git status *", ignore_case=True)  # Windows
    assert not prompts.wildcard_match("Git Status", "git status *", ignore_case=False)
    assert not prompts.wildcard_match("git statusx", "git status", ignore_case=False)


def test_a_saved_rule_covers_a_prefix_only_when_it_covers_everything_after_it():
    assert prompts.rule_covers_prefix("git status *", "git status")
    assert prompts.rule_covers_prefix("git *", "git status")
    assert prompts.rule_covers_prefix("*", "git status")
    assert not prompts.rule_covers_prefix("git status", "git status")  # Desktop saves the exact command: still asks for -sb
    assert not prompts.rule_covers_prefix("git status --short", "git status")


# --- the review ------------------------------------------------------------------------------------

def test_proposes_the_read_only_commands_approved_in_enough_sessions(desktop):
    populate(desktop)
    review = prompts.collect(desktop.path, days=30, min_sessions=3, now=NOW)
    assert [(p.prefix, p.sessions) for p in review.proposals] == [("git status", 4), ("python -m pytest", 3)]
    assert review.proposals[0].last_seen == NOW - timedelta(hours=12)  # the most recent run
    assert review.already_allowed == ["git log"]
    assert review.too_few == [("ls", 2)]
    assert review.not_read_only[:2] == ["npm install", "cd"]  # by sessions; chained `cd src && ls` is a `cd`
    assert "cat" not in review.not_read_only and "git diff" not in review.not_read_only  # failed, and too old
    assert "src/main.py" not in review.not_read_only  # not the bash tool
    assert review.commands == 5 + 3 + 2 + 5 + 3 + 3  # completed bash calls inside the window
    assert [p.pattern for p in review.proposals] == ["git status *", "python -m pytest *"]
    assert json.loads(prompts.snippet(review.proposals)) == {
        "permission": {"bash": {"git status *": "allow", "python -m pytest *": "allow"}}}


def test_min_sessions_and_the_day_window_are_the_cutoffs(desktop):
    populate(desktop)
    review = prompts.collect(desktop.path, days=30, min_sessions=2, now=NOW)
    assert [p.prefix for p in review.proposals] == ["git status", "python -m pytest", "ls"] and review.too_few == []
    review = prompts.collect(desktop.path, days=60, min_sessions=3, now=NOW)
    assert [p.prefix for p in review.proposals] == ["git status", "git diff", "python -m pytest"]
    review = prompts.collect(desktop.path, days=1, min_sessions=3, now=NOW)
    assert review.proposals == [] and review.commands == 1  # only s4's git status is that recent
    assert review.too_few == [("git status", 1)]


def test_a_rule_saved_for_the_exact_command_does_not_hide_the_prefix(desktop):
    for s in ("s1", "s2", "s3"):
        desktop.bash(s, "git status")
    desktop.saved("git status")  # what Desktop saves on "always": that exact text
    review = prompts.collect(desktop.path, now=NOW)
    assert [p.prefix for p in review.proposals] == ["git status"] and review.already_allowed == []
    desktop.saved("git status *")  # a glob the user wrote themselves
    review = prompts.collect(desktop.path, now=NOW)
    assert review.proposals == [] and review.already_allowed == ["git status"]


def test_a_rule_in_one_project_does_not_hide_the_prefix_for_another(desktop):
    for s in ("s1", "s2"):
        desktop.bash(s, "git status", project_id="p1")
    desktop.bash("s3", "git status", project_id="p2")
    desktop.saved("git status *", project_id="p1")
    review = prompts.collect(desktop.path, now=NOW)
    assert [p.prefix for p in review.proposals] == ["git status"]  # p2 still asks
    desktop.saved("*", project_id="p2")
    assert prompts.collect(desktop.path, now=NOW).already_allowed == ["git status"]


def test_v2_session_messages_count_too_and_a_call_in_both_tables_counts_once(desktop):
    call = desktop.bash("s1", "git status", v2=False)
    desktop.bash("s1", "git status", v2=True, call_id=call)  # the same call, projected into both tables
    desktop.bash("s2", "git status -sb", v2=True)
    desktop.bash("s3", "git status", v2=True)
    desktop.bash("s4", "git status", v2=True, status="error")
    review = prompts.collect(desktop.path, now=NOW)
    assert review.commands == 3 and [(p.prefix, p.sessions) for p in review.proposals] == [("git status", 3)]


def test_caveats_for_prefixes_whose_flags_can_write(desktop):
    for s in ("s1", "s2", "s3"):
        desktop.bash(s, "git branch -a")
        desktop.bash(s, "find . -name '*.py'")
    review = prompts.collect(desktop.path, now=NOW)
    text = prompts.render(review, Path("C:/cfg/opencode.jsonc"))
    assert "git branch *" in text and "also allows `git branch -D <name>`" in text
    assert "also allows `find -delete`" in text


def test_render_has_the_table_the_snippet_and_where_it_goes(desktop):
    populate(desktop)
    review = prompts.collect(desktop.path, now=NOW)
    text = prompts.render(review, Path(r"C:\Users\me\AppData\Roaming\BharatCode\Config\opencode.jsonc"))
    assert "Command           Sessions  Last seen" in text
    assert "\n  git status               4  " in text and "\n  python -m pytest         3  " in text
    assert r"Paste this into C:\Users\me\AppData\Roaming\BharatCode\Config\opencode.jsonc" in text
    assert '"git status *": "allow"' in text and '"python -m pytest *": "allow"' in text
    assert "That file is yours; spare-gpu doesn't edit it." in text
    assert "Left out: 1 already allowed by a rule you saved in Desktop (git log); " in text
    assert "1 that only read but ran in fewer than 3 sessions (ls in 2); " in text
    assert "not on the read-only list (npm install, cd)" in text
    assert "\u2014" not in text


def test_render_when_there_is_nothing_to_propose(desktop):
    assert "No bash commands finished in the last 30 days" in prompts.render(prompts.collect(desktop.path, now=NOW), Path("x"))
    desktop.bash("s1", "git status")
    text = prompts.render(prompts.collect(desktop.path, now=NOW), Path("x"))
    assert "1 bash command(s) finished in the last 30 days, but none that only reads ran in 3 or more sessions" in text
    assert "Lower --min or widen --days" in text and "Left out: 1 that only read but ran in fewer than 3 sessions (git status in 1)." in text


# --- the database: found, copied, never written ----------------------------------------------------

def test_desktops_database_is_only_copied_never_changed(desktop):
    populate(desktop)
    desktop.close()
    before = hashlib.sha256(desktop.path.read_bytes()).hexdigest()
    siblings = sorted(p.name for p in desktop.path.parent.iterdir())
    prompts.collect(desktop.path, now=NOW)
    assert hashlib.sha256(desktop.path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in desktop.path.parent.iterdir()) == siblings  # no journal or copy left next to it


def test_the_copy_is_made_while_desktop_holds_the_database_open(desktop):
    """Desktop keeps a connection (in WAL mode) open while it runs; a plain reader must still get
    the newest rows, which live in the -wal file until a checkpoint."""
    desktop.conn.execute("PRAGMA journal_mode = WAL")
    for s in ("s1", "s2", "s3"):
        desktop.bash(s, "git status")  # committed, but likely still in the -wal
    assert desktop.path.with_name(desktop.path.name + "-wal").exists()
    review = prompts.collect(desktop.path, now=NOW)  # desktop.conn is still open
    assert [(p.prefix, p.sessions) for p in review.proposals] == [("git status", 3)]


def test_a_plain_copy_is_the_fallback_when_the_backup_fails(desktop, monkeypatch):
    for s in ("s1", "s2", "s3"):
        desktop.bash(s, "git status")
    calls = []

    def failing_backup(source, target):
        calls.append(source)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(prompts, "_backup", failing_backup)
    review = prompts.collect(desktop.path, now=NOW)
    assert calls == [desktop.path] and [p.prefix for p in review.proposals] == ["git status"]


def test_no_database_says_where_it_looked_and_about_the_override(tmp_path):
    missing = tmp_path / "Data" / "bharatcode.db"
    with pytest.raises(prompts.PromptsError) as exc:
        prompts.collect(missing, now=NOW)
    assert str(exc.value).startswith(f"No BharatCode Desktop database at {missing}.")
    assert "GTU_BHARATCODE_DB" in str(exc.value)


def test_a_locked_database_that_cannot_be_copied_says_to_try_again(desktop, monkeypatch):
    desktop.bash("s1", "git status")
    desktop.conn.execute("BEGIN EXCLUSIVE")  # Desktop mid-write
    monkeypatch.setattr(prompts, "LOCK_WAIT_S", 0.05)

    def windows_sharing_violation(src, dst):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(prompts.shutil, "copy2", windows_sharing_violation)
    with pytest.raises(prompts.PromptsError) as exc:
        prompts.collect(desktop.path, now=NOW)
    message = str(exc.value)
    assert message.startswith(f"Couldn't read Desktop's database at {desktop.path}: database is locked")
    assert "copying the file failed too" in message and "try again in a moment, or close Desktop first" in message
    desktop.conn.execute("ROLLBACK")


def test_a_file_that_is_not_desktops_database_is_refused(tmp_path):
    other = tmp_path / "bharatcode.db"
    sqlite3.connect(other).execute("CREATE TABLE jobs (id TEXT)").connection.commit()
    with pytest.raises(prompts.PromptsError, match="doesn't look like BharatCode Desktop's database"):
        prompts.collect(other, now=NOW)


def test_database_path_honours_the_override_and_desktops_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("GTU_BHARATCODE_DB", str(tmp_path / "elsewhere.db"))
    assert prompts.database_path() == tmp_path / "elsewhere.db"
    monkeypatch.delenv("GTU_BHARATCODE_DB")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert prompts.database_path() == tmp_path / "Local" / "BharatCode" / "Data" / "bharatcode.db"
    assert prompts.database_path("beta") == tmp_path / "Local" / "BharatCode Beta" / "Data" / "bharatcode.db"
    assert prompts.database_path("dev").parts[-3] == "BharatCode Dev"
    legacy = tmp_path / "Local" / "bharatcode-local" / "Data" / "bharatcode.db"  # an install from before the rename
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"")
    assert prompts.database_path("local") == legacy
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert prompts.database_path() == tmp_path / "share" / "BharatCode" / "bharatcode.db"


# --- the command -------------------------------------------------------------------------------------

def test_the_command_prints_the_proposals_and_the_config_path(desktop, monkeypatch, tmp_path):
    populate(desktop)
    monkeypatch.setenv("GTU_BHARATCODE_DB", str(desktop.path))
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(tmp_path / "Config"))
    monkeypatch.setattr(prompts, "datetime", _FixedDatetime)
    result = runner.invoke(app, ["prompts"])
    assert result.exit_code == 0, result.output
    assert '"git status *": "allow"' in result.output and '"python -m pytest *": "allow"' in result.output
    assert str(tmp_path / "Config" / "opencode.jsonc") in result.output
    assert "Left out:" in result.output
    result = runner.invoke(app, ["prompts", "--min", "2", "--days", "30"])
    assert result.exit_code == 0 and '"ls *": "allow"' in result.output
    assert (tmp_path / "Config").exists() is False  # nothing was written anywhere


def test_the_command_fails_plainly_without_a_database(monkeypatch, tmp_path):
    monkeypatch.setenv("GTU_BHARATCODE_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(tmp_path / "Config"))
    result = runner.invoke(app, ["prompts"])
    assert result.exit_code == 1 and "No BharatCode Desktop database at" in result.output
    assert runner.invoke(app, ["prompts", "--channel", "nightly"]).exit_code == 1
    assert runner.invoke(app, ["prompts", "--min", "0"]).exit_code == 1


def test_the_command_defaults_match_the_module():
    params = inspect.signature(cli.prompts).parameters
    assert params["days"].default.default == prompts.DEFAULT_DAYS == 30
    assert params["min_sessions"].default.default == prompts.DEFAULT_MIN_SESSIONS == 3


class _FixedDatetime(datetime):
    """prompts.datetime.now() pinned to the fixture's clock, for the command test."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)
