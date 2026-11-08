"""/prompts: allow rules for the read-only commands you keep approving in BharatCode Desktop.

Desktop asks before its agents run a shell command, and remembers an "always" answer only for
that exact command text (core/src/tool/bash.ts saves `input.command` as the rule's resource).
So `git status`, `git status --short` and `git status -sb` each ask again. This reads a private
copy of Desktop's own database (bharatcode.db under its data folder), finds the commands the user
approved across many sessions, keeps the ones on a short list of commands that only read, and
prints the `permission` rules to paste into the user's opencode.jsonc. Nothing is written: not
into Desktop's database, which is only ever copied, and not into the config file, which is the
user's.

What was checked in Desktop's source (a fork of OpenCode):
- storage-paths.ts: the database is `<data>/bharatcode.db`; on Windows `<data>` is
  %LOCALAPPDATA%\\<name>\\Data, with <name> "BharatCode", "BharatCode Beta", "BharatCode Dev" or
  "BharatCode Local" by channel (older installs used "bharatcode", "bharatcode-beta", ...).
- session/sql.ts: `session` (id, project_id, ...), `message` (id, session_id, time_created, data)
  and `part` (id, message_id, session_id, time_created, data); `data` is the JSON of a V1 part:
  {"type": "tool", "callID", "tool": "bash", "state": {"status": "completed", "input":
  {"command": ...}, "time": {"start", "end"}}}. The newer `session_message` table holds the V2
  form: an "assistant" row whose data.content lists {"type": "tool", "id", "name": "bash",
  "state": {"status", "input"}}. Both are read, and a call seen in both counts once. Times are
  milliseconds since the epoch (database/schema.sql.ts).
- permission/sql.ts: saved rules are rows of `permission` (project_id, action, resource).
- util/wildcard.ts: a rule's pattern is a glob on the command text, `*` for anything, and a
  trailing " *" is optional, so "git status *" allows both `git status` and `git status -sb`.
  On Windows the match ignores case. `permission: {bash: {"<pattern>": "allow"}}` in the config
  becomes such a rule (v1/config/migrate.ts).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bc_config

DB_ENV = "GTU_BHARATCODE_DB"  # tests, and a Desktop that keeps its data somewhere unusual
DB_NAME = "bharatcode.db"
DEFAULT_DAYS = 30
DEFAULT_MIN_SESSIONS = 3
LOCK_WAIT_S = 5.0  # how long to wait for Desktop to finish a write before giving up on the copy

# The first word names the command; for these the second word (the subcommand) is part of it...
SUBCOMMAND_HEADS = frozenset({"git", "npm", "pnpm", "yarn", "python", "python3", "py", "go", "cargo", "make",
                              "dotnet", "pip"})
# ...and for these two-word starts the third word is too (`npm run test`, `python -m pytest`).
THREE_WORD_HEADS = frozenset({("npm", "run"), ("pnpm", "run"), ("yarn", "run"),
                              ("python", "-m"), ("python3", "-m"), ("py", "-m")})
PYTHON_HEADS = frozenset({"python", "python3", "py"})
EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")

# Commands that only read. A rule covers everything that starts with the prefix, so this stays
# short and deliberate: nothing here writes, deletes, installs, pushes or runs a script of its
# own choosing (the test runners run the project's own tests, which is what they are for).
READ_ONLY = frozenset({
    "git status", "git log", "git diff", "git show", "git branch", "git blame", "git --version",
    "ls", "dir", "type", "cat", "head", "tail", "wc", "grep", "rg", "find", "where", "pwd", "echo",
    "python -m pytest", "python3 -m pytest", "py -m pytest", "pytest",
    "npm test", "npm run test", "pnpm test", "pnpm run test", "yarn test", "yarn run test",
    "go test", "cargo test", "make test",
    "pip list", "node --version", "python --version", "python3 --version", "py --version",
})

# Arguments that make one of those commands write after all. A command carrying one is not
# counted as a read-only approval.
WRITE_FLAGS: dict[str, frozenset[str]] = {
    "git branch": frozenset({"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy",
                             "--force", "--set-upstream-to", "--unset-upstream", "--edit-description"}),
    "find": frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}),
}
GIT_BRANCH_LISTING = frozenset({"--list", "-l", "--contains", "--no-contains", "--merged", "--no-merged",
                                "--points-at", "--show-current", "-a", "--all", "-r", "--remotes"})
# What a rule for these prefixes also lets through; printed next to the proposal.
CAVEATS = {
    "git branch": "also allows `git branch -D <name>` and `git branch <new-name>`",
    "find": "also allows `find -delete` and `find -exec`",
}

# A command with any of these does more than its first word says (`cd src && rm -rf build`).
OPERATORS = re.compile(r"&&|\|\||[|;<>`]|\$\(|\r|\n")
TOKEN = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")  # the same split Desktop's bash tool uses


class PromptsError(Exception):
    """The database couldn't be found or read; the message says what to do."""


# --- commands to prefixes --------------------------------------------------------------------------

def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token.replace('"', "").replace("'", "")


def tokens_of(command: str) -> list[str]:
    return [_unquote(t) for t in TOKEN.findall(command)]


def _executable(token: str) -> str:
    """`C:\\Python311\\python.exe` and `./node_modules/.bin/jest` as the bare command name."""
    name = re.split(r"[\\/]", token)[-1]
    lowered = name.lower()
    if lowered.endswith(EXECUTABLE_SUFFIXES):
        lowered = lowered.rsplit(".", 1)[0]
    return lowered


@dataclass(frozen=True)
class Normalised:
    prefix: str  # what the rule would name, e.g. "git status"
    arguments: tuple[str, ...]  # the rest of the command
    compound: bool  # chained, piped or redirected: the prefix doesn't describe the whole command

    @property
    def read_only(self) -> bool:
        """Whether this one approved command only read: the prefix is on the list, the command is
        not chained with anything, and it carries no argument that makes the command write."""
        if self.compound or self.prefix not in READ_ONLY:
            return False
        if any(a in WRITE_FLAGS.get(self.prefix, ()) for a in self.arguments):
            return False
        if self.prefix.startswith("git ") and any(a.startswith("--output") for a in self.arguments):
            return False  # `git diff --output=<file>` and friends write a file
        if self.prefix == "git branch":  # a bare name creates a branch unless the command is listing
            positional = [a for a in self.arguments if not a.startswith("-")]
            if positional and not any(a in GIT_BRANCH_LISTING for a in self.arguments):
                return False
        return True


def normalise(command: str) -> Normalised | None:
    """The command as its prefix: the first word (path and .exe stripped, lower-cased), plus the
    subcommand for the tools that have one, plus the third word for `npm run <x>` and
    `python -m <module>`. None for an empty command."""
    compound = bool(OPERATORS.search(command))
    first = command.split("&&")[0].split("||")[0].split("|")[0].split(";")[0] if compound else command
    tokens = tokens_of(first)
    while tokens and (tokens[0] == "&" or re.fullmatch(r"\w+=.*", tokens[0])):
        tokens.pop(0)  # PowerShell's call operator, `FOO=bar cmd`
    if not tokens:
        return None
    head = _executable(tokens[0])
    rest = tokens[1:]
    if head == "git":  # global options before the subcommand: `git --no-pager diff`, `git -C src status`
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in ("-C", "-c", "--git-dir", "--work-tree") and rest:
                rest.pop(0)
    if head in PYTHON_HEADS:
        while rest and re.fullmatch(r"-\d[\d.]*(-\d+)?", rest[0]):
            rest.pop(0)  # the Windows launcher's version pick: `py -3.11 -m pytest`
    words = [head]
    if head in SUBCOMMAND_HEADS and rest:
        words.append(rest.pop(0))
        if tuple(words) in THREE_WORD_HEADS and rest:
            words.append(rest.pop(0))
    return Normalised(" ".join(words), tuple(rest), compound)


# --- Desktop's rule matching, ported ---------------------------------------------------------------

def wildcard_match(text: str, pattern: str, ignore_case: bool | None = None) -> bool:
    """Desktop's Wildcard.match (core/src/util/wildcard.ts): `*` is anything, `?` one character,
    and a pattern ending in " *" also matches without the tail. Case is ignored on Windows."""
    text, pattern = text.replace("\\", "/"), pattern.replace("\\", "/")
    escaped = re.sub(r"[.+^${}()|\[\]\\]", lambda m: "\\" + m.group(0), pattern).replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    if ignore_case is None:
        ignore_case = sys.platform == "win32"
    return re.fullmatch(escaped, text, re.S | (re.I if ignore_case else 0)) is not None


def rule_covers_prefix(resource: str, prefix: str) -> bool:
    """Whether a saved rule's pattern allows the prefix and everything after it (so a new rule
    would add nothing). A rule for one exact command, which is what Desktop saves, doesn't."""
    return wildcard_match(prefix, resource) and wildcard_match(prefix + " anything", resource)


# --- the database -----------------------------------------------------------------------------------

def database_path(channel: str = "stable") -> Path:
    """Where Desktop keeps bharatcode.db: GTU_BHARATCODE_DB, else the channel's data folder
    (storage-paths.ts). An older install's lower-case folder is used when the current one is absent."""
    override = os.environ.get(DB_ENV, "").strip()
    if override:
        return Path(override)
    name = bc_config.CHANNELS[channel]
    legacy = name.lower().replace(" ", "-")  # storage-paths.ts LEGACY_NAMES
    for folder in (name, legacy):
        if _default_path(folder).exists():
            return _default_path(folder)
    return _default_path(name)


def _default_path(name: str) -> Path:
    """<data root>/bharatcode.db for a product folder name, as storage-paths.ts lays it out."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / name / "Data" / DB_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name / DB_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / name / DB_NAME


def _backup(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API, reading the source read-only."""
    try:
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    except sqlite3.OperationalError:  # a WAL database whose -shm can't be made read-only: open plainly
        src = sqlite3.connect(str(source), timeout=LOCK_WAIT_S)
    try:
        src.execute("PRAGMA query_only = 1")  # belt and braces: this connection never writes
        # Python's backup() retries a locked source for ever; a plain read first gives up after
        # LOCK_WAIT_S with "database is locked", which the caller turns into a plain copy.
        src.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _plain_copy(source: Path, target: Path) -> None:
    """The fallback: the file and, when present, its -wal and -shm, which hold the newest writes."""
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        side = source.with_name(source.name + suffix)
        if side.exists():
            shutil.copy2(side, target.with_name(target.name + suffix))


def copy_database(source: Path, folder: Path) -> Path:
    """A private copy of Desktop's database in `folder`, so Desktop can keep it open and nothing
    here can touch it: the backup API first, a plain copy if that fails."""
    if not source.is_file():
        raise PromptsError(
            f"No BharatCode Desktop database at {source}. Is Desktop installed, and has it been opened "
            f"once? If it keeps its data somewhere else, point {DB_ENV} at the bharatcode.db file.")
    target = folder / DB_NAME
    try:
        _backup(source, target)
    except sqlite3.Error as first:
        for leftover in (target, target.with_name(target.name + "-journal")):
            leftover.unlink(missing_ok=True)
        try:
            _plain_copy(source, target)
        except OSError as second:
            raise PromptsError(
                f"Couldn't read Desktop's database at {source}: {first}; copying the file failed too ({second}). "
                "Desktop may be busy with it; try again in a moment, or close Desktop first.") from None
    return target


@contextmanager
def snapshot(source: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a fresh private copy of Desktop's database; the copy is deleted after."""
    folder = Path(tempfile.mkdtemp(prefix="gtu-prompts-"))
    try:
        copy = copy_database(source, folder)
        conn = sqlite3.connect(str(copy))
        conn.row_factory = sqlite3.Row
        try:
            try:
                tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            except sqlite3.DatabaseError as exc:
                raise PromptsError(f"Couldn't read Desktop's database at {source}: {exc}. Desktop may be busy "
                                   "with it; try again in a moment, or close Desktop first.") from None
            if "session" not in tables or not ({"part", "session_message"} & tables):
                raise PromptsError(f"{source} doesn't look like BharatCode Desktop's database: it has no session "
                                   "tables. Point GTU_BHARATCODE_DB at the right bharatcode.db.")
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@dataclass(frozen=True)
class Run:
    """One completed bash tool call."""
    session_id: str
    command: str
    at: datetime


def _ms_to_time(ms: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def bash_runs(conn: sqlite3.Connection, since: datetime) -> list[Run]:
    """Every bash command that completed since `since`, from the `part` table (V1 parts) and the
    `session_message` table (V2 assistant content); a call present in both counts once."""
    since_ms = int(since.timestamp() * 1000)
    tables = _tables(conn)
    seen: set[tuple[str, str]] = set()
    runs: list[Run] = []

    def add(session_id: str, call_id: str | None, state: object, fallback_ms: object, ended: object) -> None:
        if not isinstance(state, dict) or state.get("status") != "completed":
            return
        command = (state.get("input") or {}).get("command") if isinstance(state.get("input"), dict) else None
        if not isinstance(command, str) or not command.strip():
            return
        key = (session_id, call_id or f"{fallback_ms}:{command}")
        if key in seen:
            return
        at = _ms_to_time(ended) or _ms_to_time(fallback_ms)
        if at is None or at < since:
            return
        seen.add(key)
        runs.append(Run(session_id, command, at))

    if "part" in tables:
        rows = conn.execute("SELECT session_id, time_created, data FROM part WHERE time_created >= ? "
                            "AND data LIKE '%\"tool\"%'", (since_ms,))
        for row in rows:
            try:
                part = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if not isinstance(part, dict) or part.get("type") != "tool" or part.get("tool") != "bash":
                continue
            state = part.get("state")
            ended = (state.get("time") or {}).get("end") if isinstance(state, dict) and isinstance(state.get("time"), dict) else None
            add(row["session_id"], part.get("callID"), state, row["time_created"], ended)
    if "session_message" in tables:
        rows = conn.execute("SELECT session_id, time_created, data FROM session_message "
                            "WHERE type = 'assistant' AND time_created >= ? AND data LIKE '%\"tool\"%'", (since_ms,))
        for row in rows:
            try:
                message = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            content = message.get("content") if isinstance(message, dict) else None
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict) or item.get("type") != "tool" or item.get("name") != "bash":
                    continue
                time = item.get("time") if isinstance(item.get("time"), dict) else {}
                add(row["session_id"], item.get("id"), item.get("state"), row["time_created"], time.get("completed"))
    return runs


def session_projects(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: r["project_id"] for r in conn.execute("SELECT id, project_id FROM session")}


def saved_bash_rules(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(project_id, resource pattern) of every saved rule that applies to the bash tool."""
    if "permission" not in _tables(conn):
        return []
    rows = conn.execute("SELECT project_id, action, resource FROM permission").fetchall()
    return [(r["project_id"], r["resource"]) for r in rows if wildcard_match("bash", r["action"])]


# --- the review ---------------------------------------------------------------------------------------

@dataclass
class Proposal:
    prefix: str
    sessions: int
    last_seen: datetime
    caveat: str | None = None

    @property
    def pattern(self) -> str:
        return f"{self.prefix} *"


@dataclass
class Review:
    database: Path
    days: int
    min_sessions: int
    since: datetime
    commands: int = 0  # completed bash commands in the window
    proposals: list[Proposal] = field(default_factory=list)
    already_allowed: list[str] = field(default_factory=list)  # read-only prefixes a saved rule covers
    too_few: list[tuple[str, int]] = field(default_factory=list)  # read-only, but in fewer sessions
    not_read_only: list[str] = field(default_factory=list)  # approved, but not on the list


def analyse(runs: list[Run], projects: dict[str, str], rules: list[tuple[str, str]], review: Review) -> Review:
    """Fill a Review from the runs: prefixes by distinct sessions, the read-only ones kept."""
    review.commands = len(runs)
    read_only_sessions: dict[str, set[str]] = {}
    other_sessions: dict[str, set[str]] = {}
    last: dict[str, datetime] = {}
    ran_in: dict[str, set[str]] = {}  # prefix -> project ids whose sessions ran it read-only
    for run in runs:
        norm = normalise(run.command)
        if norm is None:
            continue
        if norm.read_only:
            read_only_sessions.setdefault(norm.prefix, set()).add(run.session_id)
            ran_in.setdefault(norm.prefix, set()).add(projects.get(run.session_id, ""))
            last[norm.prefix] = max(last.get(norm.prefix, run.at), run.at)
        else:
            other_sessions.setdefault(norm.prefix, set()).add(run.session_id)
    for prefix, sessions in sorted(read_only_sessions.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        covered_in = {project for project, resource in rules if rule_covers_prefix(resource, prefix)}
        if ran_in[prefix] and ran_in[prefix] <= covered_in:
            review.already_allowed.append(prefix)
        elif len(sessions) < review.min_sessions:
            review.too_few.append((prefix, len(sessions)))
        else:
            review.proposals.append(Proposal(prefix, len(sessions), last[prefix], CAVEATS.get(prefix)))
    review.not_read_only = sorted((p for p in other_sessions if p not in read_only_sessions),
                                  key=lambda p: (-len(other_sessions[p]), p))
    return review


def collect(database: Path, days: int = DEFAULT_DAYS, min_sessions: int = DEFAULT_MIN_SESSIONS,
            now: datetime | None = None) -> Review:
    """Copy Desktop's database and review the bash commands approved in the last `days` days.
    Raises PromptsError when there is no database or it can't be read."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    review = Review(database=database, days=days, min_sessions=min_sessions, since=since)
    with snapshot(database) as conn:
        runs = bash_runs(conn, since)
        return analyse(runs, session_projects(conn), saved_bash_rules(conn), review)


# --- output -----------------------------------------------------------------------------------------------

def snippet(proposals: list[Proposal]) -> str:
    """The `permission` block to paste: one glob rule per prefix, in Desktop's pattern form."""
    return json.dumps({"permission": {"bash": {p.pattern: "allow" for p in proposals}}}, indent=2)


def _local(at: datetime) -> str:
    return at.astimezone().strftime("%d %b %H:%M")


def _listing(prefixes: list[str], limit: int = 6) -> str:
    shown = ", ".join(prefixes[:limit])
    return shown + (f" and {len(prefixes) - limit} more" if len(prefixes) > limit else "")


def render(review: Review, config_path: Path) -> str:
    """The table, the snippet and where it goes."""
    window = f"the last {review.days} days" if review.days != 1 else "the last day"
    lines: list[str] = []
    if review.commands == 0:
        lines.append(f"No bash commands finished in {window} in BharatCode Desktop ({review.database}), "
                     "so there is nothing to propose.")
    elif not review.proposals:
        lines.append(f"{review.commands} bash command(s) finished in {window}, but none that only reads ran in "
                     f"{review.min_sessions} or more sessions, so there is nothing to propose. "
                     "Ask /prompts for fewer sessions (`/prompts --min 2`) or more days (`/prompts 90`) to look further.")
    else:
        lines.append(f"Commands that only read, approved in {review.min_sessions} or more sessions in {window} "
                     f"({review.commands} bash commands in all):")
        lines.append("")
        width = max(len("Command"), *(len(p.prefix) for p in review.proposals))
        lines.append(f"  {'Command':<{width}}  Sessions  Last seen")
        for p in review.proposals:
            note = f"  ({p.caveat})" if p.caveat else ""
            lines.append(f"  {p.prefix:<{width}}  {p.sessions:>8}  {_local(p.last_seen)}{note}")
        lines.append("")
        lines.append(f'Paste this into {config_path} (merge it into the "permission" block if the file has one):')
        lines.append("")
        lines.append(snippet(review.proposals))
        lines.append("")
        lines.append("That file is yours; spare-gpu doesn't edit it. A rule allows every command that starts with "
                     "its prefix, chained commands included (`git status && ...`), which is why only commands that "
                     "read are listed. Drop any line you'd rather keep being asked about. Desktop reads the file "
                     "when it starts, so restart it after pasting.")
    left_out = []
    if review.already_allowed:
        left_out.append(f"{len(review.already_allowed)} already allowed by a rule you saved in Desktop "
                        f"({_listing(review.already_allowed)})")
    if review.too_few:
        left_out.append(f"{len(review.too_few)} that only read but ran in fewer than {review.min_sessions} sessions "
                        f"({_listing([f'{p} in {n}' for p, n in review.too_few])})")
    if review.not_read_only:
        left_out.append(f"{len(review.not_read_only)} not on the read-only list "
                        f"({_listing(review.not_read_only)})")
    if left_out:
        lines.append("")
        lines.append("Left out: " + "; ".join(left_out) + ".")
    return "\n".join(lines)
