"""`.gtu/settings.json` and `gtu init`: the file is detected and written once, read by jobs, the
budget and the router, validated with a named fix, and an environment variable still wins."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gpu_token_utilization import router, settings
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.cli import app
from gpu_token_utilization.jobs import JobConfig
from gpu_token_utilization.store import Store

from conftest import git

cli = CliRunner()


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    for key, (env, _) in settings.KEYS.items():
        monkeypatch.delenv(env, raising=False)
    return git_repo


def written(project):
    return json.loads((project / ".gtu" / "settings.json").read_text(encoding="utf-8"))


# --- gtu init ----------------------------------------------------------------------------------

@pytest.mark.parametrize("files, command, why", [
    ({"pyproject.toml": "[project]\nname='x'\n"}, "python -m pytest", "found pyproject.toml"),
    ({"package.json": json.dumps({"scripts": {"test": "jest"}})}, "npm test", "package.json has a test script"),
    ({"package.json": json.dumps({"scripts": {"build": "tsc"}}), "go.mod": "module x\n"}, "go test ./...", "found go.mod"),
    ({"Cargo.toml": "[package]\n"}, "cargo test", "found Cargo.toml"),
    ({"Makefile": "build:\n\tcc\n\ntest:\n\t./run\n"}, "make test", "Makefile has a test target"),
    ({"tests/test_a.py": "def test_a():\n    pass\n"}, "python -m pytest", "found test_*.py files"),
    ({}, "python -m pytest", "nothing recognisable; edit test_command if your tests run another way"),
])
def test_init_detects_the_test_command(project, files, command, why):
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text, encoding="utf-8")
    result = cli.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert f"Wrote {project / '.gtu' / 'settings.json'}" in result.output and f"Test command: {why}." in result.output
    doc = written(project)
    assert doc["test_command"] == command
    assert JobConfig.load(project).test_command == tuple(command.split())  # it is allowed by its own allowlist
    assert doc["job_token_budget"] == 200_000 and doc["preferred_model"] == "deepseek-v4.1-flash" and doc["switch_safe"] == []
    assert doc["_help"].startswith("spare-gpu's settings for this project")


def test_init_keeps_an_existing_file_unless_forced_and_makes_it_committable(project):
    assert cli.invoke(app, ["init"]).exit_code == 0
    path = project / ".gtu" / "settings.json"
    path.write_text(json.dumps({"test_command": "make check", "allowed_commands": ["make check"]}), encoding="utf-8")
    result = cli.invoke(app, ["init"])
    assert result.exit_code == 0 and "already exists; left as it is (--force rewrites it)" in result.output
    assert "test_command: make check" in result.output and written(project)["test_command"] == "make check"
    result = cli.invoke(app, ["init", "--force"])
    assert result.exit_code == 0 and f"Rewrote {path}" in result.output and written(project)["test_command"] == "python -m pytest"
    # git sees the settings file and nothing else in .gtu
    with Store(project / ".gtu" / "gtu.db"):
        pass
    assert (project / ".gtu" / ".gitignore").read_text(encoding="utf-8") == settings.IGNORE
    status = git(project, "status", "--porcelain", "--untracked-files=all")
    assert ".gtu/settings.json" in status and "gtu.db" not in status and ".gtu/.gitignore" in status


def test_init_upgrades_our_old_ignore_file_but_not_the_users(project):
    folder = project / ".gtu"
    folder.mkdir()
    (folder / ".gitignore").write_text("*\n", encoding="utf-8")  # what earlier versions wrote
    cli.invoke(app, ["init"])
    assert (folder / ".gitignore").read_text(encoding="utf-8") == settings.IGNORE
    (folder / ".gitignore").write_text("*\n!mine.txt\n", encoding="utf-8")
    cli.invoke(app, ["init", "--force"])
    assert (folder / ".gitignore").read_text(encoding="utf-8") == "*\n!mine.txt\n"


def test_init_from_a_subfolder_writes_at_the_repo_root(project, monkeypatch):
    monkeypatch.chdir(project / "src")
    assert cli.invoke(app, ["init"]).exit_code == 0
    assert (project / ".gtu" / "settings.json").is_file() and not (project / "src" / ".gtu").exists()


# --- reading -----------------------------------------------------------------------------------

def test_jobs_budget_and_router_read_the_file_and_the_environment_wins(project, monkeypatch):
    (project / ".gtu").mkdir()
    (project / ".gtu" / "settings.json").write_text(json.dumps({
        "test_command": ["npm", "test", "--", "--ci"], "allowed_commands": ["npm test", "make lint"],
        "test_timeout": 45, "agent_timeout": 120.5, "job_token_budget": 50_000,
        "preferred_model": "qwen-3.8-27b", "switch_safe": ["tests"]}), encoding="utf-8")
    config = JobConfig.load(project)
    assert config.test_command == ("npm", "test", "--", "--ci") and config.allowed_commands == ("npm test", "make lint")
    assert (config.test_timeout, config.agent_timeout) == (45.0, 120.5)
    assert Budgets.load(project).per_job == 50_000
    folder = project / ".gtu"
    assert router.switch_safe("tests", router.load_policy(folder)) and not router.switch_safe("docs", router.load_policy(folder))
    assert router.preferred_model("deepseek-v4.1-flash", folder) == "qwen-3.8-27b"
    assert router.preferred_model("deepseek-v4.1-flash") == "deepseek-v4.1-flash"  # no folder: the file isn't read
    monkeypatch.setenv("GTU_TEST_COMMAND", "make lint")  # allowed by the file's own allowlist
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "7")
    monkeypatch.setenv("GTU_PREFERRED_MODEL", "bharatcode/deepseek-v4.1-flash")
    assert JobConfig.load(project).test_command == ("make", "lint") and Budgets.load(project).per_job == 7
    assert router.preferred_model("qwen-3.8-27b", folder) == "deepseek-v4.1-flash"
    monkeypatch.setenv("GTU_TEST_COMMAND", "pytest -q")  # without the file, only the default allowlist applies
    assert JobConfig.from_env().test_command == ("pytest", "-q") and JobConfig.load(None).allowed_commands == JobConfig().allowed_commands


def test_without_a_file_the_defaults_apply(project):
    assert not settings.load(project).present and settings.load(None).values == {}
    assert JobConfig.load(project) == JobConfig() and Budgets.load(project).per_job == 200_000


@pytest.mark.parametrize("doc, message", [
    ("{not json", "doesn't parse"),
    ("[]", "must hold a JSON object"),
    ({"test_cmd": "pytest"}, "unknown setting(s) test_cmd; the keys are test_command, allowed_commands"),
    ({"test_command": ""}, "test_command must be a command string or a non-empty list of words"),
    ({"allowed_commands": "pytest"}, "allowed_commands must be a list of command prefixes"),
    ({"test_timeout": "fast"}, "test_timeout must be a number of seconds above 0"),
    ({"job_token_budget": 0}, "job_token_budget must be a whole number of tokens above 0"),
    ({"job_token_budget": True}, "job_token_budget must be a whole number"),
    ({"switch_safe": ["tests", "nope"]}, "switch_safe names unknown job kind(s) nope; the kinds are tests, docs, review, fix, green, implement"),
    ({"green_rounds": 11}, "green_rounds must be a whole number of rounds from 1 to 10"),
    ({"test_command": "rm -rf /"}, "test command 'rm -rf /' is not in the allowlist"),
])
def test_a_broken_file_is_refused_with_the_fix(project, doc, message):
    (project / ".gtu").mkdir()
    (project / ".gtu" / "settings.json").write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match=message.replace("(", "\\(").replace(")", "\\)").replace("[", "\\[").replace("]", "\\]")):
        JobConfig.load(project)
    result = cli.invoke(app, ["start", "--dry-run"])
    assert result.exit_code == 1 and message.split(";")[0] in result.output
    assert "job settings:" in cli.invoke(app, ["doctor"]).output  # the doctor names it too, and goes on
    # the router doesn't crash on it: routing just sees no settings
    assert router.preferred_model("deepseek-v4.1-flash", project / ".gtu") == "deepseek-v4.1-flash"


def test_the_doctor_says_where_the_settings_come_from(project, monkeypatch):
    monkeypatch.setenv("GTU_BHARATCODE_CONFIG_DIR", str(project / "cfg"))
    (project / "cfg").mkdir()
    out = cli.invoke(app, ["doctor"]).output
    assert "PASS: job settings from defaults (no settings file; `gtu init` writes one): test command 'python -m pytest', 200,000 tokens per job" in out
    cli.invoke(app, ["init"])
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "999")
    out = cli.invoke(app, ["doctor"]).output
    assert f"PASS: job settings from settings file {project / '.gtu' / 'settings.json'}: test command 'python -m pytest', 999 tokens per job; overridden by the environment: job_token_budget" in out


def test_the_cli_store_is_the_projects_from_any_subfolder(project, monkeypatch):
    with Store(Store.default_path(project / "src")) as store:
        store.add_job("review", "src", str(project))
    assert Store.default_path(project / "src") == project / ".gtu" / "gtu.db"
    monkeypatch.chdir(project / "src")
    assert "review" in cli.invoke(app, ["list"]).output
    monkeypatch.setenv("GTU_DB", str(project / "elsewhere.db"))
    assert Store.default_path(project / "src") == project / "elsewhere.db"
