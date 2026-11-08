"""BharatCode's public model list (the pack's install reads it to name the heavy and the standard
model), and the environment spare-gpu gives the commands a job runs."""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel

BASE_URL = "https://bharatcode.ai/api/model/v1"
API_KEY_ENV = "BHARATCODE_API_KEY"


class ApiError(Exception):
    """The model list could not be read."""


class Model(BaseModel):
    id: str
    display_name: str | None = None
    access_tier: str | None = None


class ModelList(BaseModel):
    data: list[Model]


def env_without_key(**extra: str) -> dict[str, str]:
    """The environment for subprocesses we start (model-written tests and the commands we run),
    minus any BharatCode API key the user has set: none of them need it, and none should see it."""
    env = {k: v for k, v in os.environ.items() if k.upper() != API_KEY_ENV}
    env.update(extra)
    return env


def public_models(client: httpx.Client | None = None, base_url: str = BASE_URL, timeout: float = 30.0) -> list[Model]:
    """GET /models with no key: the list is public."""
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = http.get(f"{base_url}/models")
        except httpx.HTTPError as exc:
            raise ApiError(f"could not reach BharatCode ({type(exc).__name__})") from None
    finally:
        if owns_client:
            http.close()
    if response.status_code != 200:
        raise ApiError(f"unexpected response (HTTP {response.status_code})")
    try:
        return ModelList.model_validate(response.json()).data
    except ValueError:
        raise ApiError("unexpected response body from /models") from None
