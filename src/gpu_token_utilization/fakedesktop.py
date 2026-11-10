"""A fake BharatCode Desktop server, in-process (httpx.MockTransport), for tests.

Speaks the few routes spare-gpu uses: /global/health, POST /session, POST
/session/{id}/prompt_async, GET /session/{id}/message, GET /session/status and POST
/session/{id}/abort. A session plays its scripted steps as its messages are polled, each step an
assistant message with a step-finish part (and a tool part per tool or file written), like the
real server. A step can write files into the session's folder, as the model's edit tool would,
and take several polls to finish. With `calls`, each new session plays its own list of steps (the
first session the first list, and so on; the last list repeats), so a job whose sessions must
differ can be scripted. The job's opencode.json, if one is in the folder when the
prompt arrives, is recorded with the session. A session can be prompted again (an /auto
conversation): each prompt plays the next list of steps after a new user message. `chats` are
the user's own chats, busy with a running call to the named tool; `pending` holds permission
requests waiting for the user (GET /permission). A prompt on a model in `failing` never
progresses: its session's status is "retry", as Desktop shows while it retries a failing model.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

PASSWORD = "fake-desktop-password"
URL = "http://127.0.0.1:4096"


class FakeClock:
    """Fake time for the scheduler's waits: `sleep` records and advances, nothing really sleeps."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.start = start or datetime.now(timezone.utc)

    def __call__(self) -> float:
        return self.now

    def utcnow(self) -> datetime:
        """Wall-clock view of the same fake time."""
        return self.start + timedelta(seconds=self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Step:
    tokens: int = 1000
    tool: tuple[str, str] | None = None  # (tool name, file path)
    text: str = ""
    writes: dict[str, str] = field(default_factory=dict)  # path in the session's folder -> content, written as the step starts
    polls: int = 1  # message polls the step takes to finish


@dataclass
class FakeDesktop:
    steps: list[Step] = field(default_factory=lambda: [Step(text="done")])
    calls: list[list[Step]] | None = None  # one list of steps per session, in order; the last repeats
    error: str | None = None  # set on the last assistant message
    stalled: bool = False  # the last reply never completes while the session goes idle (seen 5 Nov)
    password: str = PASSWORD
    sessions: dict = field(default_factory=dict)  # id -> {"directory", "title", "prompt", "config", "revealed", "aborted"}
    healthy: bool = True
    chats: dict = field(default_factory=dict)  # session id -> the tool it's running a call to
    pending: list = field(default_factory=list)  # permission requests, e.g. {"id": "per_1", "sessionID": ...}
    prompts: int = 0  # prompts sent so far, across sessions (picks each one's steps from `calls`)
    failing: set = field(default_factory=set)  # model ids BharatCode answers "Model unavailable" for

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def agent(self, **kw):
        """A DesktopAgent talking to this fake, not sleeping between polls unless given `sleep`."""
        from .desktop import Connection, DesktopAgent

        kw.setdefault("sleep", lambda seconds: None)
        return DesktopAgent(Connection(URL, "opencode", self.password), transport=self.transport(), **kw)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401)
        path, method = request.url.path, request.method
        directory = request.url.params.get("directory")
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "dev"}) if self.healthy else httpx.Response(500)
        if path == "/session" and method == "POST":
            sid = f"ses_{len(self.sessions) + 1}"
            body = json.loads(request.content or b"{}")
            self.sessions[sid] = {"directory": directory, "title": body.get("title"), "prompt": None, "config": None,
                                  "revealed": 0, "ticks": 0, "aborted": False, "permission": body.get("permission"),
                                  "steps": [], "prompts": [], "starts": []}
            return httpx.Response(200, json={"id": sid, "directory": directory, "title": body.get("title")})
        if path == "/session/status":
            busy = {sid: ({"type": "retry", "attempt": 1, "message": "Model unavailable.", "next": 10000}
                          if self._stuck(s) else {"type": "busy"})
                    for sid, s in self.sessions.items()
                    if s["prompt"] and not s["aborted"] and s["revealed"] < len(s["steps"])}
            return httpx.Response(200, json=busy | {sid: {"type": "busy"} for sid in self.chats})
        if path == "/permission" and method == "GET":
            return httpx.Response(200, json=self.pending)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "session" and parts[1] in self.chats and parts[2] == "message":
            running = {"id": "prt_call", "type": "tool", "tool": self.chats[parts[1]], "state": {"status": "running"}}
            return httpx.Response(200, json=[{"info": {"id": "msg_u", "role": "user"}, "parts": []},
                                             {"info": {"id": "msg_a", "role": "assistant", "time": {"created": 1}},
                                              "parts": [running]}])
        if len(parts) >= 3 and parts[0] == "session" and parts[1] in self.sessions:
            s = self.sessions[parts[1]]
            if parts[2] == "prompt_async" and method == "POST":
                s["prompt"] = json.loads(request.content)
                s["prompts"].append(s["prompt"])
                s["aborted"] = False  # a new turn after an aborted one
                s["starts"].append(len(s["steps"]))
                s["steps"] = s["steps"] + list(
                    self.calls[min(self.prompts, len(self.calls) - 1)] if self.calls else self.steps)
                self.prompts += 1
                config = Path(s["directory"] or ".") / "opencode.json"
                s["config"] = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else None
                return httpx.Response(204)
            if parts[2] == "abort" and method == "POST":
                s["aborted"] = True
                s["steps"] = s["steps"][: s["revealed"]]  # the rest of that turn never happens
                return httpx.Response(200, json=True)
            if parts[2] == "message" and method == "GET":
                self._advance(s)
                return httpx.Response(200, json=self._messages(parts[1], s))
        return httpx.Response(404)

    def _stuck(self, s: dict) -> bool:
        return ((s["prompt"] or {}).get("model") or {}).get("modelID") in self.failing

    def _advance(self, s: dict) -> None:
        if not s["prompt"] or s["aborted"] or s["revealed"] >= len(s["steps"]) or self._stuck(s):
            return
        step = s["steps"][s["revealed"]]
        if s["ticks"] == 0:
            for name, content in step.writes.items():
                target = Path(s["directory"]) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="")  # exactly as given, like an edit tool
        s["ticks"] += 1
        if s["ticks"] >= step.polls:
            s["revealed"] += 1
            s["ticks"] = 0

    def _messages(self, sid: str, s: dict) -> list[dict]:
        out = []
        for i, step in enumerate(s["steps"][: s["revealed"]], 1):
            if i - 1 in s["starts"]:
                out.append({"info": {"id": f"msg_u{i}", "role": "user"}, "parts": []})
            last = i == len(s["steps"])
            parts = [{"id": f"prt_{i}_a", "type": "step-start"}]
            tools = ([step.tool] if step.tool else []) + [("edit", name) for name in step.writes]
            for j, (tool, file) in enumerate(tools):
                parts.append({"id": f"prt_{i}_b{j}", "type": "tool", "tool": tool,
                              "state": {"status": "completed", "input": {"filePath": file}}})
            if step.text:
                parts.append({"id": f"prt_{i}_c", "type": "text", "text": step.text})
            parts.append({"id": f"prt_{i}_d", "type": "step-finish",
                          "tokens": {"input": step.tokens - 10, "output": 10, "reasoning": 0,
                                     "cache": {"read": 0, "write": 0}, "total": step.tokens}})
            ends_turn = i == len(s["steps"]) or i in s["starts"]
            info = {"id": f"msg_{i}", "role": "assistant", "time": {"created": i, "completed": i},
                    "finish": "stop" if ends_turn else "tool-calls"}
            if last and self.stalled:
                del info["time"]["completed"]
                parts = parts[:1]
            if last and self.error:
                info["error"] = {"name": "UnknownError", "data": {"message": self.error}}
            out.append({"info": info, "parts": parts})
        if s["prompt"] and len(s["steps"]) > s["revealed"] and s["revealed"] in s["starts"]:
            out.append({"info": {"id": f"msg_u{s['revealed'] + 1}", "role": "user"}, "parts": []})  # not answered yet
        return out
