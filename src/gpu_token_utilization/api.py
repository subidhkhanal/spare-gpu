"""Minimal BharatCode API access used by `gtu check`.

The full client (backoff, truncation handling, request log) arrives in Phase 1.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

BASE_URL = "https://bharatcode.ai/api/model/v1"
API_KEY_ENV = "BHARATCODE_API_KEY"
# BharatCode paces accounts and asked automated clients to allow long waits.
DEFAULT_TIMEOUT = 180.0
TIMEOUT_ENV = "GTU_TIMEOUT"


def env_number(name: str, default: float) -> float:
    """A positive number from the environment, or the default if unset."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def request_timeout() -> float:
    """Seconds to wait for a model response (GTU_TIMEOUT, default 180)."""
    return env_number(TIMEOUT_ENV, DEFAULT_TIMEOUT)


class ApiKeyMissing(Exception):
    """BHARATCODE_API_KEY is not set."""


class ApiError(Exception):
    """The API could not be reached or rejected the request."""


class Model(BaseModel):
    id: str
    display_name: str | None = None
    access_tier: str | None = None


class ModelList(BaseModel):
    data: list[Model]


@dataclass
class CheckResult:
    models: list[Model]
    elapsed_ms: float
    request_id: str | None


@dataclass
class KeyCheck:
    model: str
    elapsed_ms: float
    request_id: str | None
    total_tokens: int | None


def get_api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise ApiKeyMissing(f"{API_KEY_ENV} is not set")
    return key


def list_models(
    base_url: str = BASE_URL,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> CheckResult:
    """GET /models. The key is read from the environment and only sent as a header."""
    key = get_api_key()
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        start = time.perf_counter()
        try:
            response = http.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            raise ApiError(f"could not reach BharatCode ({type(exc).__name__})") from None
        elapsed_ms = (time.perf_counter() - start) * 1000
    finally:
        if owns_client:
            http.close()

    request_id = response.headers.get("x-request-id")
    if response.status_code in (401, 403):
        raise ApiError(f"API key was rejected (HTTP {response.status_code})")
    if response.status_code == 503:
        raise ApiError("BharatCode is busy (HTTP 503), try again in a few seconds")
    if response.status_code != 200:
        raise ApiError(f"unexpected response (HTTP {response.status_code})")
    try:
        parsed = ModelList.model_validate(response.json())
    except ValueError:
        raise ApiError("unexpected response body from /models") from None
    return CheckResult(models=parsed.data, elapsed_ms=elapsed_ms, request_id=request_id)


PREFERRED_MODEL = "qwen-3.8-27b"


def verify_key(
    models: list[Model],
    base_url: str = BASE_URL,
    timeout: float | None = None,
    client: httpx.Client | None = None,
) -> KeyCheck:
    """Send a 1-token POST /chat/completions. GET /models does not check the key, this does."""
    key = get_api_key()
    timeout = request_timeout() if timeout is None else timeout
    ids = [m.id for m in models]
    model = PREFERRED_MODEL if PREFERRED_MODEL in ids or not ids else ids[0]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "reasoning_effort": "none",
        "stream": False,
    }
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        start = time.perf_counter()
        try:
            response = http.post(
                f"{base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"could not reach BharatCode ({type(exc).__name__})") from None
        elapsed_ms = (time.perf_counter() - start) * 1000
    finally:
        if owns_client:
            http.close()

    request_id = response.headers.get("x-request-id")
    if response.status_code in (401, 403):
        raise ApiError(f"API key was rejected (HTTP {response.status_code})")
    if response.status_code == 503:
        raise ApiError("BharatCode is busy (HTTP 503), try again in a few seconds")
    if response.status_code != 200:
        raise ApiError(f"key check failed (HTTP {response.status_code})")
    total_tokens = None
    try:
        usage = response.json().get("usage") or {}
        total_tokens = usage.get("total_tokens")
    except ValueError:
        pass
    return KeyCheck(model=model, elapsed_ms=elapsed_ms, request_id=request_id, total_tokens=total_tokens)
