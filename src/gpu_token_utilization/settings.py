"""Per-project settings: <project>/.gtu/settings.json, written by `gtu init`, read by every job.

The file holds what used to live only in environment variables: the test command and the
commands a job may run, the timeouts, the token budget per job, the preferred model and the job
kinds that may move to the faster model. Precedence, lowest first: spare-gpu's defaults, the
file, then the GTU_* environment variable, so a one-off override still works from a terminal.
The file is meant to be committed: .gtu/.gitignore keeps everything else in the folder out of git.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import worktree
from .store import JOB_KINDS

FILE = "settings.json"
HELP_KEY = "_help"
HELP = ("spare-gpu's settings for this project. A GTU_* environment variable overrides its key for one run. "
        "`gtu init --force` rewrites this file from what it detects.")

# key -> (environment variable, what it means). Keep README's Settings table in step with this.
KEYS: dict[str, tuple[str, str]] = {
    "test_command": ("GTU_TEST_COMMAND", "the command that runs the project's tests (a string, or a list of words)"),
    "allowed_commands": ("GTU_ALLOWED_COMMANDS", "command prefixes a job may run; the test command must be one of them"),
    "test_timeout": ("GTU_TEST_TIMEOUT", "seconds one test run may take"),
    "agent_timeout": ("GTU_AGENT_TIMEOUT", "seconds one Desktop session of a job may take"),
    "job_token_budget": ("GTU_JOB_TOKEN_BUDGET", "tokens one job may use before it pauses"),
    "preferred_model": ("GTU_PREFERRED_MODEL", "the model a queued job runs on unless the other starts it sooner"),
    "switch_safe": ("GTU_SWITCH_SAFE", "job kinds that may move to the faster model, on top of routing_policy.json"),
}

IGNORE = "*\n!.gitignore\n!settings.json\n"  # .gtu/.gitignore: the folder stays out of git, the settings don't


class SettingsError(ValueError):
    """The settings file can't be used; the message says what to fix."""


def project_root(path: Path) -> Path:
    """The folder a project's .gtu lives in: the top of the git repo containing `path`, else `path`."""
    return worktree.project_root(path)


def settings_path(root: Path) -> Path:
    return Path(root) / ".gtu" / FILE


@dataclass
class Settings:
    path: Path
    values: dict = field(default_factory=dict)
    present: bool = False

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    @property
    def test_command(self) -> tuple[str, ...] | None:
        value = self.values.get("test_command")
        if value is None:
            return None
        return tuple(shlex.split(value)) if isinstance(value, str) else tuple(value)


def load(root: Path | None) -> Settings:
    """The project's settings, validated. No file is fine (defaults apply); a broken one is not."""
    if root is None:
        return Settings(Path("settings.json"))
    path = settings_path(root)
    if not path.is_file():
        return Settings(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise SettingsError(f"{path} doesn't parse ({exc}); fix it, or delete it and run `gtu init`") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} must hold a JSON object; run `gtu init --force` to rewrite it")
    values = validate(raw, path)
    return Settings(path, values, present=True)


def validate(raw: dict, path: Path | str = FILE) -> dict:
    """The known keys with checked types; raises SettingsError naming the first problem."""
    unknown = sorted(k for k in raw if k not in KEYS and k != HELP_KEY)
    if unknown:
        raise SettingsError(f"{path}: unknown setting(s) {', '.join(unknown)}; the keys are {', '.join(KEYS)}")
    out = {}
    for key, value in raw.items():
        if key == HELP_KEY:
            continue
        problem = _problem(key, value)
        if problem:
            raise SettingsError(f"{path}: {key} {problem}")
        out[key] = value
    return out


def _problem(key: str, value) -> str | None:
    strings = lambda v: isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)  # noqa: E731
    if key == "test_command":
        if isinstance(value, str) and value.strip():
            return None
        return None if strings(value) and value else "must be a command string or a non-empty list of words"
    if key == "allowed_commands":
        return None if strings(value) else "must be a list of command prefixes (strings)"
    if key in ("test_timeout", "agent_timeout"):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else "must be a number of seconds above 0"
    if key == "job_token_budget":
        return None if isinstance(value, int) and not isinstance(value, bool) and value > 0 else "must be a whole number of tokens above 0"
    if key == "preferred_model":
        return None if isinstance(value, str) and value.strip() else "must be a model id such as deepseek-v4.1-flash"
    if key == "switch_safe":
        if not strings(value):
            return "must be a list of job kinds"
        bad = [k for k in value if k not in JOB_KINDS]
        return f"names unknown job kind(s) {', '.join(bad)}; the kinds are {', '.join(JOB_KINDS)}" if bad else None
    return "is not a setting"


# --- gtu init -------------------------------------------------------------------------------------

DETECTIONS = (
    # (file in the project root, command, how it was recognised); the first match wins
    ("pyproject.toml", "python -m pytest", "pyproject.toml"),
    ("pytest.ini", "python -m pytest", "pytest.ini"),
    ("setup.cfg", "python -m pytest", "setup.cfg"),
    ("tox.ini", "python -m pytest", "tox.ini"),
    ("package.json", "npm test", "package.json"),
    ("go.mod", "go test ./...", "go.mod"),
    ("Cargo.toml", "cargo test", "Cargo.toml"),
)


def detect_test_command(root: Path) -> tuple[str, str]:
    """(test command, why): from the project's files, else spare-gpu's default with a note."""
    root = Path(root)
    for name, command, why in DETECTIONS:
        if (root / name).is_file():
            if name == "package.json":
                try:
                    scripts = (json.loads((root / name).read_text(encoding="utf-8-sig")) or {}).get("scripts") or {}
                except ValueError:
                    scripts = {}
                if "test" not in scripts:
                    continue
                return command, "package.json has a test script"
            return command, f"found {why}"
    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^test\s*:", makefile.read_text(encoding="utf-8", errors="replace"), re.M):
        return "make test", "Makefile has a test target"
    if any(root.glob("tests/test_*.py")) or any(root.glob("test_*.py")):
        return "python -m pytest", "found test_*.py files"
    return "python -m pytest", "nothing recognisable; edit test_command if your tests run another way"


def defaults(root: Path) -> tuple[dict, str]:
    """What `gtu init` writes for this project, and why that test command."""
    from .budget import Budgets
    from .jobs import DEFAULT_ALLOWED, JobConfig
    from .router import DELEGATE_PREFERRED

    command, why = detect_test_command(root)
    allowed = list(DEFAULT_ALLOWED)
    prefix = " ".join(command.split()[:2])
    if not any(" ".join(a.split()[:2]) == prefix for a in allowed):
        allowed.append(prefix)
    config = JobConfig()
    doc = {
        HELP_KEY: HELP,
        "test_command": command,
        "allowed_commands": allowed,
        "test_timeout": config.test_timeout,
        "agent_timeout": config.agent_timeout,
        "job_token_budget": Budgets().per_job,
        "preferred_model": DELEGATE_PREFERRED,
        "switch_safe": [],
    }
    return doc, why


def ensure_ignore(folder: Path) -> None:
    """.gtu/.gitignore that keeps the folder out of git except settings.json. An older file of
    ours (just "*") is upgraded; a file the user wrote is left alone."""
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / ".gitignore"
    if not marker.exists() or marker.read_text(encoding="utf-8", errors="replace").strip() == "*":
        marker.write_text(IGNORE, encoding="utf-8")


def init(root: Path, force: bool = False) -> tuple[Path, str, dict, str]:
    """Write the settings file unless it exists (then keep it, unless `force`).
    Returns (path, "written" | "kept" | "replaced", the settings, why that test command)."""
    root = Path(root)
    path = settings_path(root)
    doc, why = defaults(root)
    if path.is_file() and not force:
        return path, "kept", load(root).values, why
    outcome = "replaced" if path.is_file() else "written"
    ensure_ignore(path.parent)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path, outcome, {k: v for k, v in doc.items() if k != HELP_KEY}, why
