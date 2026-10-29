import re

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gpu_token_utilization.cli import app

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("GTU_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n")
    return tmp_path


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


@respx.mock
def test_why_explains_the_gate(repo):
    from datetime import datetime, timezone

    from gpu_token_utilization.gate import USAGE_URL

    lane = {"active_requests": 4, "queued_requests": 6, "slot_capacity": 4, "capacity_used_pct": 100, "available": True}
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "models": [{"id": "qwen-3.8-27b", "api": lane}],
            },
        )
    )
    result = runner.invoke(app, ["why"])
    assert result.exit_code == 0, result.output
    assert "Paused (live stats): qwen-3.8-27b api lane 4/4 slots busy, 6 queued (tolerance 5)." in result.output


def test_start_without_key_fails(repo, monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1 and "BHARATCODE_API_KEY" in result.output
