"""Minimal BharatCode API access used by `gtu check` and `gtu setkey`, and where the key
comes from. The full client (backoff, truncation, request log) is in client.py."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from . import keystore

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
    """No key in BHARATCODE_API_KEY and none saved by `gtu setkey`."""


class ApiError(Exception):
    """The API could not be reached or rejected the request."""


class KeyRejected(ApiError):
    """BharatCode answered 401/403: the key itself is wrong."""


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


def clean_key(raw: str) -> str:
    """Trim whitespace and invisible byte-order marks (PowerShell pipes and some editors
    add one; "ï»¿" is the same mark mis-decoded as cp1252)."""
    return raw.replace("﻿", "").replace("ï»¿", "").strip()


def key_problem(key: str) -> str | None:
    """Why this can't be an API key, or None. Keys are plain printable ASCII; anything else
    would crash the HTTP header instead of giving a clear message."""
    if any(ch.isspace() for ch in key):
        return "it contains spaces"
    if not (key.isascii() and key.isprintable()):
        return "it contains characters that can't be in a key (often an invisible mark added when copying)"
    return None


def find_api_key() -> tuple[str, str] | None:
    """(key, where it came from): the environment variable wins, then the key saved by
    `gtu setkey`. The saved key is never copied into the environment."""
    key, source = clean_key(os.environ.get(API_KEY_ENV, "")), f"the {API_KEY_ENV} environment variable"
    if not key:
        try:
            key = clean_key(keystore.load() or "")
        except keystore.KeystoreError as exc:
            raise ApiKeyMissing(str(exc)) from None
        source = f"{keystore.LOCATION} (saved by gtu setkey)"
    if not key:
        return None
    problem = key_problem(key)
    if problem:
        raise ApiKeyMissing(f"the API key in {source} isn't usable: {problem}")
    return key, source


def env_without_key(**extra: str) -> dict[str, str]:
    """The environment for subprocesses we start (model-written tests, the BharatCode CLI and
    the commands it runs), minus our API key: none of them need it, and none should see it.
    The BharatCode CLI signs in with `bharatcode auth login`, not this variable."""
    env = {k: v for k, v in os.environ.items() if k.upper() != API_KEY_ENV}
    env.update(extra)
    return env


def get_api_key() -> str:
    found = find_api_key()
    if found is None:
        raise ApiKeyMissing("no BharatCode API key found")
    return found[0]


def list_models(
    base_url: str = BASE_URL,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
    key: str | None = None,
) -> CheckResult:
    """GET /models. The key (found as in get_api_key unless given) is only sent as a header."""
    key = key or get_api_key()
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
        raise KeyRejected(f"API key was rejected (HTTP {response.status_code})")
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
    key: str | None = None,
) -> KeyCheck:
    """Send a 1-token POST /chat/completions. GET /models does not check the key, this does."""
    key = key or get_api_key()
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
        raise KeyRejected(f"API key was rejected (HTTP {response.status_code})")
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
