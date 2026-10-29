import json

import httpx
import respx
from typer.testing import CliRunner

from gpu_token_utilization.api import BASE_URL
from gpu_token_utilization.cli import app

runner = CliRunner()
FAKE_KEY = "bc_live_test_key_not_real"
MODELS = {"data": [{"id": "qwen-3.8-27b", "display_name": "Qwen 3.8 27B", "access_tier": "standard"}]}


def mock_models():
    return respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=MODELS))


@respx.mock
def test_check_lists_models_and_verifies_key(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", FAKE_KEY)
    mock_models()
    chat = respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200, headers={"x-request-id": "req_123"}, json={"usage": {"total_tokens": 9}}
        )
    )
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    assert "qwen-3.8-27b" in result.output
    assert "API key accepted" in result.output
    assert "req_123" in result.output
    assert "9 tokens" in result.output
    request = chat.calls.last.request
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(request.content)
    assert body["max_tokens"] == 1 and body["stream"] is False and body["model"] == "qwen-3.8-27b"
    assert FAKE_KEY not in result.output


@respx.mock
def test_check_reports_missing_request_id(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", FAKE_KEY)
    mock_models()
    respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    assert "not sent" in result.output


def test_check_without_key_fails(monkeypatch):
    monkeypatch.delenv("BHARATCODE_API_KEY", raising=False)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "BHARATCODE_API_KEY" in result.output


@respx.mock
def test_check_with_rejected_key_fails(monkeypatch):
    # /models accepts any key, so the rejection arrives on /chat/completions.
    monkeypatch.setenv("BHARATCODE_API_KEY", FAKE_KEY)
    mock_models()
    respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(401, json={"error": "nope"}))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "rejected" in result.output
    assert "API key accepted" not in result.output
    assert FAKE_KEY not in result.output


@respx.mock
def test_check_when_busy_fails_with_clear_message(monkeypatch):
    monkeypatch.setenv("BHARATCODE_API_KEY", FAKE_KEY)
    mock_models()
    respx.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(503))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "busy" in result.output
