"""In-process fake of the BharatCode API for tests.

It is an httpx transport, so nothing touches the network. Queue replies to
script its behaviour: busy (503), truncated, slow, or specific token usage.
Pair it with its FakeClock so delays and backoff sleeps cost no real time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import httpx

MODELS = [
    {"id": "qwen-3.8-27b", "display_name": "Qwen 3.8 27B", "access_tier": "standard"},
    {"id": "deepseek-v4.1-flash", "display_name": "BharatCode DeepSeek V4.1 Flash", "access_tier": "heavy"},
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Reply:
    status: int = 200
    content: str = "ok"
    finish_reason: str = "stop"
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0
    delay: float = 0.0
    tool_calls: list[dict] | None = None
    retry_after: float | None = None
    headers: dict = field(default_factory=dict)


class FakeBharatCode:
    def __init__(self, api_key: str = "bc_live_fake_key", clock: FakeClock | None = None):
        self.api_key = api_key
        self.clock = clock or FakeClock()
        self.replies: deque[Reply] = deque()
        self.default = Reply()
        self.requests: list[httpx.Request] = []
        self._ids = 0

    # scripting

    def queue(self, *replies: Reply) -> FakeBharatCode:
        self.replies.extend(replies)
        return self

    def busy(self, times: int = 1, retry_after: float | None = None, status: int = 503) -> FakeBharatCode:
        return self.queue(*(Reply(status=status, retry_after=retry_after) for _ in range(times)))

    def ok(self, content: str = "ok", **usage) -> FakeBharatCode:
        return self.queue(Reply(content=content, **usage))

    def truncated(self, content: str = "partial outp", tool_call: bool = False) -> FakeBharatCode:
        calls = None
        if tool_call:
            calls = [{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.py", "con'}}]
        return self.queue(Reply(content=content, finish_reason="length", tool_calls=calls))

    def slow(self, seconds: float, **reply) -> FakeBharatCode:
        return self.queue(Reply(delay=seconds, **reply))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # handling

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            # Like the real API, /models answers without checking the key and sends no x-request-id.
            return httpx.Response(200, json={"object": "list", "data": MODELS})

        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        reply = self.replies.popleft() if self.replies else self.default
        if reply.delay:
            self.clock.advance(reply.delay)
            timeout = (request.extensions.get("timeout") or {}).get("read")
            if timeout is not None and reply.delay > timeout:
                raise httpx.ReadTimeout("fake server too slow", request=request)

        self._ids += 1
        headers = {"x-request-id": f"fake-req-{self._ids}", **reply.headers}
        if reply.status != 200:
            if reply.retry_after is not None:
                headers["retry-after"] = str(reply.retry_after)
            return httpx.Response(reply.status, headers=headers, json={"error": {"message": "model is busy"}})

        message: dict = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = reply.tool_calls
        body = {
            "id": f"chatcmpl-{self._ids}",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": reply.finish_reason}],
            "usage": {
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
                "total_tokens": reply.prompt_tokens + reply.completion_tokens,
                "completion_tokens_details": {"reasoning_tokens": reply.reasoning_tokens},
            },
        }
        return httpx.Response(200, headers=headers, json=body)
