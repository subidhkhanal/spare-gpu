"""`.gtu/settings.json`: written by hand when the defaults don't fit, read by jobs, the budget and
the router, validated with a named fix, and an environment variable still wins. Without a file
(or without a test_command in it) the test command comes from the project's own files."""

import json

import pytest

from gpu_token_utilization import doctor, router, settings
from gpu_token_utilization.budget import Budgets
from gpu_token_utilization.jobs import DEFAULT_ALLOWED, JobConfig
from gpu_token_utilization.mcp_server import SpareTools
from gpu_token_utilization.store import Store

from conftest import git


@pytest.fixture
def project(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.delenv("GTU_DB", raising=False)
    for key, (env, _) in settings.KEYS.items():
        monkeypatch.delenv(env, raising=False)
    return git_repo


def write_settings(project, doc):
    (project / ".gtu").mkdir(exist_ok=True)
    path = project / ".gtu" / "settings.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def settings_line(project):
    report = doctor.Report()
    doctor.check_settings(report, project)
    return "\n".join(str(f) for f in report.findings)


# --- the test command from the project's files -------------------------------------------------

@pytest.mark.parametrize("files, command, why", [
    ({"pyproject.toml": "[project]\nname='x'\n"}, "python -m pytest", "found pyproject.toml"),
    ({"package.json": json.dumps({"scripts": {"test": "jest"}})}, "npm test", "package.json has a test script"),
    ({"package.json": json.dumps({"scripts": {"build": "tsc"}}), "go.mod": "module x\n"}, "go test ./...", "found go.mod"),
    ({"Cargo.toml": "[package]\n"}, "cargo test", "found Cargo.toml"),
    ({"Makefile": "build:\n\tcc\n\ntest:\n\t./run\n"}, "make test", "Makefile has a test target"),
    ({"tests/test_a.py": "def test_a():\n    pass\n"}, "python -m pytest", "found test_*.py files"),
    ({}, "python -m pytest", "nothing recognisable; edit test_command if your tests run another way"),
])
def test_the_test_command_is_detected_from_the_projects_files(project, files, command, why):
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text, encoding="utf-8")
    assert settings.detect_test_command(project) == (command, why)
    config = JobConfig.load(project)  # no settings file at all
    assert config.test_command == tuple(command.split())  # and it is allowed: load() would raise otherwise


def test_a_detected_command_is_added_to_the_allowlist_only_when_missing(project):
    assert JobConfig.load(project).test_command == ("python", "-m", "pytest")
    assert JobConfig.load(project).allowed_commands == DEFAULT_ALLOWED
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    config = JobConfig.load(project)
    assert config.test_command == ("npm", "test") and config.allowed_commands == DEFAULT_ALLOWED  # already allowed
    (project / "package.json").unlink()
    (project / "go.mod").write_text("module x\n", encoding="utf-8")
    config = JobConfig.load(project)
    assert config.test_command == ("go", "test", "./...") and config.allowed_commands == DEFAULT_ALLOWED
    (project / "go.mod").unlink()
    (project / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    config = JobConfig.load(project)
    assert config.test_command == ("cargo", "test") and config.allowed_commands == (*DEFAULT_ALLOWED, "cargo test")


def test_a_settings_files_test_command_wins_over_the_detected_one(project):
    (project / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    write_settings(project, {"test_command": "pytest -q"})
    config = JobConfig.load(project)
    assert config.test_command == ("pytest", "-q") and "cargo test" not in config.allowed_commands
    # a file that names its own allowlist but no test command: the detected command must be in that list
    write_settings(project, {"allowed_commands": ["cargo test"]})
    config = JobConfig.load(project)
    assert config.test_command == ("cargo", "test") and config.allowed_commands == ("cargo test",)
    write_settings(project, {"allowed_commands": ["make lint"]})
    with pytest.raises(ValueError, match="test command 'cargo test' is not in the allowlist"):
        JobConfig.load(project)


def test_a_hand_written_settings_file_is_committable_and_the_rest_of_gtu_is_not(project):
    write_settings(project, {"test_command": "make check", "allowed_commands": ["make check"]})
    with Store(project / ".gtu" / "gtu.db"):
        pass
    assert (project / ".gtu" / ".gitignore").read_text(encoding="utf-8") == settings.IGNORE
    status = git(project, "status", "--porcelain", "--untracked-files=all")
    assert ".gtu/settings.json" in status and "gtu.db" not in status and ".gtu/.gitignore" in status


# --- reading -----------------------------------------------------------------------------------

def test_jobs_budget_and_router_read_the_file_and_the_environment_wins(project, monkeypatch):
    write_settings(project, {
        "test_command": ["npm", "test", "--", "--ci"], "allowed_commands": ["npm test", "make lint"],
        "test_timeout": 45, "agent_timeout": 120.5, "job_token_budget": 50_000,
        "preferred_model": "qwen-3.8-27b", "switch_safe": ["tests"]})
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
    write_settings(project, doc)
    with pytest.raises(ValueError, match=message.replace("(", "\\(").replace(")", "\\)").replace("[", "\\[").replace("]", "\\]")):
        JobConfig.load(project)
    assert f"FAIL: job settings: " in settings_line(project) and message.split(";")[0] in settings_line(project)  # the doctor names it, and goes on
    # the router doesn't crash on it: routing just sees no settings
    assert router.preferred_model("deepseek-v4.1-flash", project / ".gtu") == "deepseek-v4.1-flash"


def test_the_doctor_says_where_the_settings_come_from(project, monkeypatch):
    assert ("PASS: job settings from defaults (no settings file; settings.json in the project's .gtu folder changes them): "
            "test command 'python -m pytest', 200,000 tokens per job") in settings_line(project)
    (project / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert "test command 'cargo test'" in settings_line(project)  # the detected command, as jobs will run it
    path = write_settings(project, {"test_command": "python -m pytest"})
    monkeypatch.setenv("GTU_JOB_TOKEN_BUDGET", "999")
    assert (f"PASS: job settings from settings file {path}: test command 'python -m pytest', 999 tokens per job; "
            "overridden by the environment: job_token_budget") in settings_line(project)


def test_the_store_is_the_projects_from_any_subfolder(project, monkeypatch):
    with Store(Store.default_path(project / "src")) as store:
        store.add_job("review", "src", str(project))
    assert Store.default_path(project / "src") == project / ".gtu" / "gtu.db"
    assert "review src" in SpareTools(project / "src").work_status()  # the MCP server started in a subfolder finds it
    monkeypatch.setenv("GTU_DB", str(project / "elsewhere.db"))
    assert Store.default_path(project / "src") == project / "elsewhere.db"
