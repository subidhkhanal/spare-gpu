"""BharatCode chat client.

One request in flight at a time, polite backoff when busy, truncation detection,
token accounting and a log row for every attempt (including the x-request-id).
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .api import BASE_URL, get_api_key
from .store import RequestLog, Store

DEFAULT_MODEL = "qwen-3.8-27b"
MAX_OUTPUT_TOKENS = 32_000
CHAT_ENDPOINT = "/chat/completions"
# The docs only name 503 ("model is busy"). Treating 429 the same way is our guess.
BUSY_STATUSES = (429, 503)


class BharatCodeError(Exception):
    """A request failed in a way retrying will not fix."""


class AuthError(BharatCodeError):
    """The API key was rejected (401/403)."""


class BusyError(BharatCodeError):
    """BharatCode stayed busy (or unreachable) after all retries."""


@dataclass
class RetryPolicy:
    max_retries: int = 4
    base_delay: float = 5.0
    max_delay: float = 120.0
    jitter: float = 0.25

    def delay(self, attempt: int, retry_after: float | None = None, rng: Callable[[], float] = random.random) -> float:
        """Wait before retry number `attempt` (1-based): doubling, jittered, capped."""
        wait = self.base_delay * (2 ** (attempt - 1)) * (1 + self.jitter * rng())
        if retry_after is not None:
            wait = max(wait, retry_after)
        return min(wait, self.max_delay)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_body(cls, body: dict) -> Usage:
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return cls(
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            reasoning_tokens=details.get("reasoning_tokens") or 0,
            total_tokens=usage.get("total_tokens") or 0,
        )


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    truncated: bool
    usage: Usage
    request_id: str | None
    elapsed_ms: float
    attempts: int
    tool_calls: list[dict] = field(default_factory=list)
    # Tool calls from a truncated reply. Possibly incomplete, so they must never be executed.
    dropped_tool_calls: list[dict] = field(default_factory=list)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class BharatCodeClient:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        api_key: str | None = None,
        timeout: float = 120.0,
        retry: RetryPolicy | None = None,
        store: Store | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        rng: Callable[[], float] = random.random,
    ):
        self._key = api_key or get_api_key()
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self.retry = retry or RetryPolicy()
        self.store = store
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"BharatCodeClient(base_url={str(self._http.base_url)!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BharatCodeClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        reasoning_effort: str = "none",
        tools: list[dict] | None = None,
        job_id: str | None = None,
        step_id: int | None = None,
    ) -> ChatResult:
        """Send the full history (the API is stateless) and return one non-streamed reply."""
        if not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        body: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "stream": False,
            "store": False,
        }
        if tools:
            body["tools"] = tools

        def log(attempt: int, **fields) -> None:
            if self.store is not None:
                self.store.log_request(
                    RequestLog(endpoint=CHAT_ENDPOINT, model=model, attempt=attempt, job_id=job_id, step_id=step_id, **fields)
                )

        # One request in flight at a time, never parallel.
        with self._lock:
            attempt = 0
            while True:
                attempt += 1
                start = self._clock()
                try:
                    response = self._http.post(CHAT_ENDPOINT, json=body, headers={"Authorization": f"Bearer {self._key}"})
                except httpx.TimeoutException:
                    log(attempt, elapsed_ms=(self._clock() - start) * 1000, error="timeout")
                    self._wait_or_give_up(attempt, None, "timed out")
                    continue
                except httpx.TransportError as exc:
                    log(attempt, elapsed_ms=(self._clock() - start) * 1000, error=f"network: {type(exc).__name__}")
                    self._wait_or_give_up(attempt, None, "could not be reached")
                    continue

                elapsed_ms = (self._clock() - start) * 1000
                request_id = response.headers.get("x-request-id")
                status = response.status_code

                if status in BUSY_STATUSES:
                    log(attempt, status_code=status, elapsed_ms=elapsed_ms, request_id=request_id, error="busy")
                    self._wait_or_give_up(attempt, _retry_after(response), f"is busy (HTTP {status})")
                    continue
                if status in (401, 403):
                    log(attempt, status_code=status, elapsed_ms=elapsed_ms, request_id=request_id, error="auth")
                    raise AuthError(f"API key was rejected (HTTP {status})")
                if status != 200:
                    snippet = response.text[:200]
                    log(attempt, status_code=status, elapsed_ms=elapsed_ms, request_id=request_id, error=snippet)
                    raise BharatCodeError(f"request failed (HTTP {status}): {snippet}")

                try:
                    result = self._parse(response.json(), request_id, elapsed_ms, attempt)
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    log(attempt, status_code=status, elapsed_ms=elapsed_ms, request_id=request_id, error="bad body")
                    raise BharatCodeError("unexpected response body from /chat/completions") from None
                u = result.usage
                log(
                    attempt,
                    status_code=status,
                    elapsed_ms=elapsed_ms,
                    request_id=request_id,
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    reasoning_tokens=u.reasoning_tokens,
                    total_tokens=u.total_tokens,
                    finish_reason=result.finish_reason,
                    truncated=result.truncated,
                )
                return result

    def _wait_or_give_up(self, attempt: int, retry_after: float | None, what: str) -> None:
        if attempt > self.retry.max_retries:
            raise BusyError(f"BharatCode {what}; gave up after {attempt} attempts")
        self._sleep(self.retry.delay(attempt, retry_after, self._rng))

    @staticmethod
    def _parse(body: dict, request_id: str | None, elapsed_ms: float, attempts: int) -> ChatResult:
        choice = body["choices"][0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        truncated = finish_reason == "length"
        tool_calls = list(message.get("tool_calls") or [])
        dropped: list[dict] = []
        if truncated and tool_calls:
            dropped, tool_calls = tool_calls, []
        return ChatResult(
            content=message.get("content") or "",
            finish_reason=finish_reason,
            truncated=truncated,
            usage=Usage.from_body(body),
            request_id=request_id,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            tool_calls=tool_calls,
            dropped_tool_calls=dropped,
        )
