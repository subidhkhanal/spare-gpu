import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization import cli
from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.cli import app
from gpu_token_utilization.client import RetryPolicy
from gpu_token_utilization.demo import pick_file

from conftest import git

runner = CliRunner()
CHAT = f"{BASE_URL}/chat/completions"
CALC = "def add(a, b):\n    return a + b\n\n\ndef mean(values):\n    return sum(values) / len(values)\n" * 6  # ~500 bytes


def reply(content="## Likely bugs\n- `src/calc.py:6` mean([]) divides by zero", status=200):
    return httpx.Response(
        status,
        headers={"x-request-id": "req-demo-1", "x-bharatcode-queue-wait-ms": "4200"},
        json={"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 300, "completion_tokens": 60, "total_tokens": 360}},
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    monkeypatch.setattr(cli, "DEMO_RETRY", RetryPolicy(max_retries=1, base_delay=0, max_delay=0))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(CALC)
    return tmp_path


# --- picking a file ----------------------------------------------------------------------

def test_picks_a_small_source_file_and_skips_tests_and_junk(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n" * 250)  # ~1.5 KB: ideal
    (tmp_path / "src" / "tiny.py").write_text("x = 1\n")  # too small to be interesting
    (tmp_path / "src" / "huge.py").write_text("x = 1\n" * 5000)  # too big for a quick demo
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helpers.py").write_text("x = 1\n" * 250)
    (tmp_path / "test_calc.py").write_text("x = 1\n" * 250)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("x = 1\n" * 250)
    (tmp_path / "notes.md").write_text("x\n" * 750)
    (tmp_path / "blob.py").write_bytes(b"\0" * 1500)
    assert pick_file(tmp_path) == tmp_path / "src" / "calc.py"


def test_in_a_git_repo_only_tracked_files_are_considered(tmp_path):
    (tmp_path / "tracked.py").write_text("x = 1\n" * 100)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "tracked.py")
    git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "untracked.py").write_text("x = 1\n" * 250)  # closer to ideal, but not tracked
    assert pick_file(tmp_path) == tmp_path / "tracked.py"


def test_nothing_suitable_returns_none(tmp_path):
    (tmp_path / "README.md").write_text("hello")
    (tmp_path / "empty.py").write_text("")
    assert pick_file(tmp_path) is None


def test_a_repo_with_only_a_tiny_file_still_gets_a_demo(tmp_path):
    # found on a clean-machine run: a new repo's only file was ~70 bytes and the demo gave up
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    assert pick_file(tmp_path) == tmp_path / "calc.py"


# --- the command -------------------------------------------------------------------------

@respx.mock
def test_demo_reviews_a_file_and_explains_next_steps(project):
    route = respx.post(CHAT).mock(return_value=reply())
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Reviewing src/calc.py" in out and "sends one request now" in out
    assert "mean([]) divides by zero" in out
    assert "360 tokens" in out and "4.2 s waiting in BharatCode's queue" in out and "req-demo-1" in out
    assert "gtu add tests src/calc.py" in out and "gtu start" in out and "own copy of the project" in out
    sent = json.loads(route.calls.last.request.content)
    assert "src/calc.py:1| def add(a, b):" in sent["messages"][1]["content"]
    assert sent["max_tokens"] == 700
    saved = list((project / ".gtu" / "demo").glob("review-*.md"))
    assert len(saved) == 1 and "divides by zero" in saved[0].read_text()
    assert (project / ".gtu" / ".gitignore").read_text() == "*\n"


@respx.mock
def test_demo_needs_no_git_and_creates_no_branches(project):
    respx.post(CHAT).mock(return_value=reply())
    assert runner.invoke(app, ["demo"]).exit_code == 0
    assert not (project / ".git").exists()


@respx.mock
def test_demo_in_a_dirty_git_repo_still_works_and_changes_nothing(project):
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    (project / "src" / "calc.py").write_text(CALC + "# uncommitted edit\n")
    respx.post(CHAT).mock(return_value=reply())
    assert runner.invoke(app, ["demo"]).exit_code == 0
    assert git(project, "branch", "--list", "gtu/*") == ""
    assert git(project, "status", "--porcelain") == "M src/calc.py"


@respx.mock
def test_demo_with_an_explicit_file(project):
    (project / "other.py").write_text("def f():\n    return 42\n")
    route = respx.post(CHAT).mock(return_value=reply())
    result = runner.invoke(app, ["demo", "other.py"])
    assert result.exit_code == 0, result.output
    assert "other.py:2|     return 42" in json.loads(route.calls.last.request.content)["messages"][1]["content"]


def test_demo_without_a_key_says_where_to_get_one(project, monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY")
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1 and "no BharatCode API key found" in result.output
    assert "bharatcode.ai/account" in result.output
    assert '$env:BHARATCODE_API_KEY = "<your key>"' in result.output


def test_demo_without_a_key_says_so_before_looking_for_a_file(tmp_path, monkeypatch):
    # found on a clean-machine run: with no key and no suitable file, it complained about the file
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1 and "no BharatCode API key found" in result.output
    assert "path/to/file.py" not in result.output


@respx.mock
def test_demo_with_a_rejected_key(project):
    respx.post(CHAT).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1 and "rejected your API key" in result.output


@respx.mock
def test_demo_when_busy_gives_up_quickly_and_says_so(project):
    route = respx.post(CHAT).mock(return_value=httpx.Response(503))
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1 and "busy right now" in result.output
    assert route.call_count == 2  # one try plus one retry, then a clear message


def test_demo_with_nothing_to_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BHARATCODE_API_KEY", "bc_live_test_key_not_real")
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1 and "gtu demo path/to/file.py" in result.output


def test_demo_key_never_printed(project):
    with respx.mock:
        respx.post(CHAT).mock(return_value=reply())
        result = runner.invoke(app, ["demo"])
    assert "bc_live_test_key_not_real" not in result.output
