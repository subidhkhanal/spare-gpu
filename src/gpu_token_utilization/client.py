"""BharatCode chat client.

One request in flight at a time, polite backoff when busy, truncation detection,
token accounting and a log row for every attempt (including the x-request-id).
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from .api import BASE_URL, env_number, get_api_key, request_timeout
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
    # Patient by default, as BharatCode asked of automated clients under account pacing:
    # 8 retries waiting about 5, 10, 20, 40, 80, 160, 300, 300 s (~15 min in all), each ±20%.
    max_retries: int = 8
    base_delay: float = 5.0
    max_delay: float = 300.0
    jitter: float = 0.2

    @classmethod
    def from_env(cls) -> RetryPolicy:
        d = cls()
        return cls(
            max_retries=int(env_number("GTU_MAX_RETRIES", d.max_retries)),
            base_delay=env_number("GTU_RETRY_BASE", d.base_delay),
            max_delay=env_number("GTU_RETRY_MAX_DELAY", d.max_delay),
            jitter=env_number("GTU_RETRY_JITTER", d.jitter),
        )

    def delay(self, attempt: int, retry_after: float | None = None, rng: Callable[[], float] = random.random) -> float:
        """Wait before retry number `attempt` (1-based): doubling, ±jitter, capped at max_delay."""
        wait = self.base_delay * (2 ** (attempt - 1))
        wait *= 1 + self.jitter * (2 * rng() - 1)
        if retry_after is not None:
            wait = max(wait, retry_after)
        return min(wait, self.max_delay)

    def total_wait(self) -> float:
        """Longest the waits add up to before giving up (jitter ignored)."""
        return sum(min(self.base_delay * 2 ** i, self.max_delay) for i in range(self.max_retries))


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
    # How long BharatCode's scheduler held the request before serving it, as it reports.
    queue_wait_ms: int | None = None


# BharatCode's scheduler reports how long it held each request. Not in the public docs.
SCHEDULER_HEADERS = {
    "x-bharatcode-queue-wait-ms": "queue_wait_ms",
    "x-bharatcode-scheduler-requested-at-ms": "scheduler_requested_at_ms",
    "x-bharatcode-scheduler-admitted-at-ms": "scheduler_admitted_at_ms",
}


def scheduler_headers(response: httpx.Response) -> dict:
    """The scheduler timing headers present on a response, as ints."""
    found = {}
    for header, name in SCHEDULER_HEADERS.items():
        try:
            found[name] = int(float(response.headers[header]))
        except (KeyError, ValueError):
            pass
    return found


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
        timeout: float | None = None,
        retry: RetryPolicy | None = None,
        store: Store | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        rng: Callable[[], float] = random.random,
        on_retry: Callable[[str], None] | None = None,
    ):
        self._key = api_key or get_api_key()
        self.timeout = request_timeout() if timeout is None else timeout
        self._http = httpx.Client(base_url=base_url, timeout=self.timeout, transport=transport)
        self.retry = retry or RetryPolicy.from_env()
        self.store = store
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        # Told about every wait before a retry, so a long backoff never looks like a hang.
        self.on_retry = on_retry or (lambda message: None)

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
        gate: dict | None = None,
    ) -> ChatResult:
        """Send the full history (the API is stateless) and return one non-streamed reply.

        `gate` holds the capacity gate's view at send time (gate_source, queue_at_send,
        active_at_send) and is copied into every logged attempt.
        """
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

        # Per-attempt facts: when it was sent, and BharatCode's scheduler headers once it answers.
        attempt_meta: dict = {}

        def log(attempt: int, **fields) -> None:
            if self.store is not None:
                self.store.log_request(
                    RequestLog(
                        endpoint=CHAT_ENDPOINT, model=model, attempt=attempt, job_id=job_id, step_id=step_id,
                        **(gate or {}), **attempt_meta, **fields,
                    )
                )

        # One request in flight at a time, never parallel.
        with self._lock:
            attempt = 0
            while True:
                attempt += 1
                attempt_meta.clear()
                attempt_meta["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
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
                attempt_meta.update(scheduler_headers(response))

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
                result.queue_wait_ms = attempt_meta.get("queue_wait_ms")
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
        wait = self.retry.delay(attempt, retry_after, self._rng)
        self.on_retry(f"BharatCode {what}. Waiting {wait:.0f}s before retry {attempt} of {self.retry.max_retries}.")
        self._sleep(wait)

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
