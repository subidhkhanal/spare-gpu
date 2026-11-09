"""Runs job steps through BharatCode Desktop's own local server.

Desktop runs BharatCode's server (the OpenCode HTTP API) on 127.0.0.1 with a password made for
each launch, and starts MCP servers such as ours as its child processes. They inherit
OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD, but not the port, so the port is found as
the one a parent process listens on (or given in GTU_DESKTOP_URL). A job step is then one Desktop
session:
- created in the job's worktree (`?directory=`), so it shows up in Desktop like any other chat;
- with the job's deny-first permission file (agent.permission_config), written into the worktree;
- with the model chosen per task (the router's "sticky per task" rule);
- polled for its steps, so tokens are reported as they come in and the token budget, the
  timeout and a cancel from the chat all abort the session.
It uses Desktop's sign-in, so spare-gpu needs no login of its own for it. Jobs only run while
Desktop is open: the server lives inside it.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import httpx

from .agent import CONFIG_FILE, DEFAULT_MODEL, AgentResult, AgentStep, permission_config

URL_ENV = "GTU_DESKTOP_URL"
USER_ENV = "OPENCODE_SERVER_USERNAME"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
HEALTH = "/global/health"
# runner -> its launcher -> MCP server -> its launcher -> Desktop's server process -> Desktop
ANCESTORS = 6
WEAK_CANDIDATES = 8  # ports with little evidence of being a server, tried after the likely ones
POLL_RETRIES = 5
STALL_POLLS = 15  # polls a session may sit idle with an unfinished reply before the step gives up


DESKTOP_NEEDED = ("spare-gpu's jobs run inside BharatCode Desktop: open this project in Desktop and ask "
                  "spare-gpu there.")


class DesktopNotFound(Exception):
    """Not running inside BharatCode Desktop, or its server didn't answer; the message says which."""


@dataclass
class Connection:
    url: str
    username: str
    password: str

    def client(self, timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.url, auth=(self.username, self.password), timeout=timeout,
                            transport=transport, trust_env=False)


# --- finding Desktop's server -------------------------------------------------------------------

def _process_parents() -> dict[int, int]:
    """Windows only (spare-gpu is Windows-only): pid -> parent pid for every process, from a snapshot."""
    if os.name != "nt":
        return {}

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong),
                    ("cntThreads", ctypes.c_ulong), ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260)]

    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    parents: dict[int, int] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel.CloseHandle(ctypes.c_void_p(snapshot))
    return parents


def loopback_ports(netstat: str) -> tuple[dict[int, list[int]], set[int]]:
    """(pid -> 127.0.0.1 ports it serves or is connected to, the ports that look like servers'),
    from `netstat -ano`. Desktop's server socket isn't reliably listed as listening: on 4 Nov it
    showed up as owned by process 0, and minutes later not at all, while connections to it were
    open. So a port also looks like a server's when several connections use it (a client's own
    end is used by one connection only)."""
    rows = [(int(local), int(remote), state, int(pid)) for local, remote, state, pid in re.findall(
        r"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+:(\d+)\s+(\S+)\s+(\d+)\s*$", netstat, re.M)]
    uses: dict[int, int] = {}
    for local, remote, state, _ in rows:
        if state == "LISTENING":
            uses[local] = uses.get(local, 0) + 2
        else:
            uses[local] = uses.get(local, 0) + 1
            uses[remote] = uses.get(remote, 0) + 1
    found: dict[int, list[int]] = {}
    for local, remote, state, pid in rows:
        if not pid:
            continue
        for port in ((local,) if state == "LISTENING" else (local, remote)):
            if port and port not in found.setdefault(pid, []):
                found[pid].append(port)
    return found, {port for port, n in uses.items() if n > 2}


def _netstat() -> str:
    try:
        return subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20,
                              errors="replace").stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def candidate_urls(pid: int | None = None, parents: dict[int, int] | None = None,
                   netstat: str | None = None) -> list[str]:
    """Loopback servers that Desktop's processes serve or talk to: first those of this process's
    nearest ancestors, then those of the rest of their process tree (Desktop's window and network
    processes keep connections to its server). discover() checks each with Desktop's password."""
    parents = _process_parents() if parents is None else parents
    ports, servers = loopback_ports(_netstat() if netstat is None else netstat)
    ancestors, current = [], pid or os.getpid()
    for _ in range(ANCESTORS):
        current = parents.get(current)
        if not current:
            break
        ancestors.append(current)
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    family, todo = list(ancestors), list(ancestors)
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in family:
                family.append(child)
                todo.append(child)
    likely = [p for member in family for p in ports.get(member, []) if p in servers]
    others = [p for member in family for p in ports.get(member, []) if p not in servers]
    ordered = list(dict.fromkeys(likely)) + list(dict.fromkeys(others))[:WEAK_CANDIDATES]
    return [f"http://127.0.0.1:{port}" for port in ordered]


def discover(env: dict | None = None, candidates: Callable[[], list[str]] = candidate_urls,
             transport: httpx.BaseTransport | None = None) -> Connection:
    env = os.environ if env is None else env
    password = env.get(PASSWORD_ENV)
    if not password:
        raise DesktopNotFound("spare-gpu wasn't started by BharatCode Desktop (no Desktop server password)")
    username = env.get(USER_ENV) or "opencode"
    urls = [env[URL_ENV]] if env.get(URL_ENV) else candidates()
    for url in urls:
        conn = Connection(url.rstrip("/"), username, password)
        try:
            with conn.client(timeout=5.0, transport=transport) as http:
                response = http.get(HEALTH)
            if response.status_code == 200 and response.json().get("healthy") is True:
                return conn
        except (httpx.HTTPError, ValueError):
            continue
    raise DesktopNotFound("couldn't reach BharatCode Desktop's server" + (f" (tried {', '.join(urls)})" if urls else ""))


def default_model(conn: Connection, transport: httpx.BaseTransport | None = None) -> str | None:
    """The BharatCode model Desktop uses by default (e.g. 'qwen-3.8-27b'), or None."""
    try:
        with conn.client(timeout=10.0, transport=transport) as http:
            data = http.get("/provider").json()
        return (data.get("default") or {}).get("bharatcode")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


# --- running a job step as a Desktop session ----------------------------------------------------

def session_rules(edit: bool, commands: tuple[str, ...]) -> list[dict]:
    """The job's permission rules as a Desktop session ruleset (last match wins, like the config's):
    the global deny first, then what the job may do. Sent with the session itself, so they apply
    whatever config Desktop has loaded for the folder; also hides spare-gpu's own tools by name,
    so a job can't queue more jobs. The skill list in the prompt follows the agent's (config)
    permissions only, so the opencode.json file is still written too."""
    rules = []
    for permission, value in permission_config(edit, commands, {})["permission"].items():
        if isinstance(value, str):
            rules.append({"permission": permission, "pattern": "*", "action": value})
        else:
            rules += [{"permission": permission, "pattern": pattern, "action": action}
                      for pattern, action in value.items()]
    rules.append({"permission": "spare-gpu_*", "pattern": "*", "action": "deny"})
    return rules


AUTO_DENY = {"permission": "spare-gpu_*", "pattern": "*", "action": "deny"}


def calling_session(conn: Connection, workdir: Path, tool: str, transport: httpx.BaseTransport | None = None) -> str | None:
    """The chat that is calling `tool` right now (its full name, e.g. 'spare-gpu_spare_auto'): a
    busy session in `workdir` whose last messages hold a running call to it. MCP doesn't say which
    chat called, so this is how /auto keeps one background session per chat. None unless exactly one."""
    where = {"directory": str(workdir)}
    found = []
    try:
        with conn.client(timeout=10.0, transport=transport) as http:
            statuses = http.get("/session/status", params=where)
            statuses.raise_for_status()
            for sid, status in statuses.json().items():
                if (status or {}).get("type") == "idle":
                    continue
                listed = http.get(f"/session/{sid}/message", params=where)
                if listed.status_code != 200:
                    continue
                for message in listed.json()[-3:]:
                    if any(part.get("type") == "tool" and part.get("tool") == tool
                           and (part.get("state") or {}).get("status") in ("pending", "running")
                           for part in message.get("parts") or []):
                        found.append(sid)
                        break
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return found[0] if len(found) == 1 else None


def _model_ref(model: str) -> dict:
    provider, _, model_id = model.partition("/")
    return {"providerID": provider, "modelID": model_id} if model_id else {"providerID": "bharatcode", "modelID": provider}


def _error_text(error: dict) -> str:
    return (error.get("data") or {}).get("message") or error.get("name") or json.dumps(error)[:300]


class DesktopAgent:
    """Runs a job step (or a named agent, for the benchmarks) as a Desktop session and follows it."""

    label = "BharatCode Desktop"

    def with_model(self, model: str) -> DesktopAgent:
        """The same agent on another model (the router picks one per job)."""
        return DesktopAgent(self.conn, model=model, timeout=self.timeout, poll=self.poll, transport=self._transport,
                            sleep=self._sleep, clock=self._clock, mcp_servers=self.mcp_servers)

    def __init__(self, conn: Connection, model: str = DEFAULT_MODEL, timeout: float = 900.0, poll: float = 2.0,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, mcp_servers: Mapping[str, dict] | None = None):
        self.conn = conn
        self.model = model
        self.timeout = timeout
        self.poll = poll
        self.mcp_servers = mcp_servers  # None: the user's config is read when a job runs
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    def run(self, workdir: Path, prompt: str, *, edit: bool = True, commands: tuple[str, ...] = (),
            token_budget: int | None = None, on_step: Callable[[AgentStep], None] = lambda step: None,
            should_stop: Callable[[], bool] = lambda: False) -> AgentResult:
        """A job step: a session under the job's permission file and rules, on this agent's model."""
        workdir = Path(workdir)
        config = workdir / CONFIG_FILE
        config.write_text(json.dumps(permission_config(edit, commands, self.mcp_servers), indent=2), encoding="utf-8")
        try:
            return self._session(workdir, prompt, {"permission": session_rules(edit, commands)},
                                 {"model": _model_ref(self.model)}, token_budget, on_step, should_stop)
        finally:
            config.unlink(missing_ok=True)

    def run_agent(self, workdir: Path, agent: str | None, prompt: str, *, model: str | None = None,
                  token_budget: int | None = None,
                  on_step: Callable[[AgentStep], None] = lambda step: None) -> AgentResult:
        """One of Desktop's agents (a pack lead for the benchmarks; None: Desktop's default agent)
        on a prompt, as a session in `workdir`. The agent keeps its own permissions, and its own
        model unless `model` pins one: nothing of a job's applies."""
        extra = ({"agent": agent} if agent else {}) | ({"model": _model_ref(model)} if model else {})
        return self._session(Path(workdir), prompt, {}, extra, token_budget, on_step, lambda: False)

    def server_version(self) -> str | None:
        """The version Desktop's server reports, or None."""
        try:
            with self.conn.client(timeout=10.0, transport=self._transport) as http:
                return http.get(HEALTH).json().get("version")
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def converse(self, workdir: Path, session: str | None, prompt: str, *, title: str, model: str,
                 on_step: Callable[[AgentStep], None] = lambda step: None,
                 on_approval: Callable[[], None] = lambda: None) -> tuple[str | None, AgentResult]:
        """One turn of an ongoing conversation (/auto): `prompt` sent to `session` (a new one titled
        `title` when None, or when that session is gone) in `workdir`, on `model`, with Desktop's
        default agent and the user's own permissions; only spare-gpu's tools are denied, so it can't
        call /auto again. Returns the session's id and this turn's result. A turn that runs past the
        timeout is left running (result.stopped == "timeout"), never aborted; `on_approval` is
        called once when the session waits for the user's approval."""
        return self._session(Path(workdir), prompt, {"permission": [AUTO_DENY]}, {"model": _model_ref(model)},
                             None, on_step, lambda: False, session=session, title=title, keep_running=True,
                             on_approval=on_approval)

    def _session(self, workdir: Path, prompt: str, session_extra: dict, prompt_extra: dict, token_budget,
                 on_step, should_stop, *, session: str | None = None, title: str | None = None,
                 keep_running: bool = False, on_approval: Callable[[], None] | None = None):
        """Run one prompt as a session and follow it. Returns the result; with keep_running (an
        ongoing conversation), the session's id and the result."""
        result = AgentResult(exit_code=None, text="")
        where = {"directory": str(workdir)}
        try:
            with self.conn.client(transport=self._transport) as http:
                skip = 0
                if session:
                    earlier = http.get(f"/session/{session}/message", params=where)
                    if earlier.status_code == 404:
                        session = None  # deleted in Desktop: start a new one
                    else:
                        earlier.raise_for_status()
                        skip = len(earlier.json())
                if not session:
                    created = http.post("/session", params=where, json={
                        "title": title or (f"spare-gpu: {prompt.splitlines()[0][:60]}" if prompt.strip() else "spare-gpu"),
                        **session_extra,
                    })
                    created.raise_for_status()
                    session = created.json()["id"]
                sent = http.post(f"/session/{session}/prompt_async", params=where,
                                 json={**prompt_extra, "parts": [{"type": "text", "text": prompt}]})
                sent.raise_for_status()
                self._follow(http, session, where, result, token_budget, on_step, should_stop, skip=skip,
                             keep_running=keep_running, on_approval=on_approval)
        except httpx.HTTPError as exc:
            result.error = f"BharatCode Desktop's server: {exc}"
        if result.exit_code is None:
            result.exit_code = 1 if result.error or result.stopped else 0
        return (session, result) if keep_running else result

    def ask(self, workdir: Path, messages: list[dict], *, token_budget: int | None = None,
            on_step: Callable[[AgentStep], None] = lambda step: None,
            should_stop: Callable[[], bool] = lambda: False) -> AgentResult:
        """A one-answer request (the review job) as a Desktop session that may read but not edit
        or run anything, so it needs Desktop's sign-in rather than spare-gpu's own API key."""
        prompt = "\n\n".join(m["content"] for m in messages if m.get("content"))
        prompt += "\n\nReply with the report itself and nothing else. Don't use any tools."
        return self.run(workdir, prompt, edit=False, commands=(), token_budget=token_budget, on_step=on_step,
                        should_stop=should_stop)

    def _follow(self, http, session, where, result, token_budget, on_step, should_stop, *, skip: int = 0,
                keep_running: bool = False, on_approval: Callable[[], None] | None = None) -> None:
        deadline = self._clock() + self.timeout
        seen: set[str] = set()
        dropped = idle_unfinished = 0
        asked = False
        while True:
            self._sleep(self.poll * (2 ** dropped))
            try:
                messages = http.get(f"/session/{session}/message", params=where)
                messages.raise_for_status()
                listed = messages.json()[skip:]  # an ongoing session's earlier turns aren't this one
                finished = self._read(listed, seen, result, on_step)
                # Each step is its own assistant message, so a completed one doesn't mean the agent
                # is done; the session's status does. Idle sessions are left out of the status map.
                busy = session in self._statuses(http, where)
                if on_approval and busy and not asked and self._waits_for_approval(http, session, where):
                    asked = True
                    on_approval()
            except httpx.TransportError:
                # A dropped local connection (seen 4 Nov: WinError 10054 mid-job while Desktop's
                # server carried on) isn't a failed job: the session keeps going, so look again.
                dropped += 1
                if dropped > POLL_RETRIES:
                    self._abort(http, session, where)
                    raise
                continue
            dropped = 0
            done = finished and not busy
            # Seen 5 Nov: after a service outage Desktop marked a session idle while its reply never
            # completed, and the job waited on it for over an hour. Idle + unfinished for a while = dead.
            replied = any((m.get("info") or {}).get("role") == "assistant" for m in listed)
            idle_unfinished = idle_unfinished + 1 if replied and not finished and not busy else 0
            if idle_unfinished >= STALL_POLLS:
                result.error = "BharatCode Desktop stopped the session without finishing its reply"
                self._abort(http, session, where)
                return
            if token_budget is not None and result.tokens >= token_budget:
                result.stopped = "budget"
            elif should_stop():
                result.stopped = "stop"
            elif self._clock() > deadline:
                result.stopped = "timeout"
            if result.stopped:
                if not (keep_running and result.stopped == "timeout"):
                    self._abort(http, session, where)
                return
            if done:
                return

    @staticmethod
    def _waits_for_approval(http, session, where) -> bool:
        """Whether the session has a permission request waiting for the user (best effort)."""
        try:
            response = http.get("/permission", params=where)
            response.raise_for_status()
            return any((p or {}).get("sessionID") == session for p in response.json())
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    @staticmethod
    def _abort(http, session, where) -> None:
        """Stop the session, best effort: a job spare-gpu gave up on shouldn't keep spending tokens."""
        try:
            http.post(f"/session/{session}/abort", params=where)
        except httpx.HTTPError:
            pass

    @staticmethod
    def _statuses(http, where) -> dict:
        response = http.get("/session/status", params=where)
        response.raise_for_status()
        return {sid: s for sid, s in response.json().items() if (s or {}).get("type") != "idle"}

    @staticmethod
    def _read(messages: list[dict], seen: set[str], result: AgentResult, on_step) -> bool:
        """Take in new steps; True once the assistant has finished its last message."""
        texts, last = [], None
        for message in messages:
            info = message.get("info") or {}
            if info.get("role") != "assistant":
                continue
            last = info
            for part in message.get("parts") or []:
                kind = part.get("type")
                if kind == "step-finish" and part.get("id") not in seen:
                    seen.add(part.get("id"))
                    tokens = part.get("tokens") or {}
                    cache = tokens.get("cache") or {}
                    step = AgentStep(
                        input_tokens=tokens.get("input") or 0, output_tokens=tokens.get("output") or 0,
                        reasoning_tokens=tokens.get("reasoning") or 0, cache_read_tokens=cache.get("read") or 0,
                        total_tokens=tokens.get("total") or sum(
                            [tokens.get("input") or 0, tokens.get("output") or 0, tokens.get("reasoning") or 0,
                             cache.get("read") or 0, cache.get("write") or 0]),
                        elapsed_ms=None, cache_write_tokens=cache.get("write") or 0,
                    )
                    result.steps.append(step)
                    on_step(step)
                elif kind == "tool" and part.get("id") not in seen and (part.get("state") or {}).get("status") in (
                        "completed", "error"):
                    seen.add(part.get("id"))
                    state_input = (part.get("state") or {}).get("input") or {}
                    target = state_input.get("command") or state_input.get("filePath") or ""
                    result.tools.append(f"{part.get('tool')} {target}".strip())
                elif kind == "text" and part.get("text"):
                    texts.append(part["text"])
        if last is None or not (last.get("time") or {}).get("completed"):
            return False
        if last.get("error"):
            result.error = _error_text(last["error"])
        # Every text part: a review's body often comes before a closing remark.
        result.text = "\n".join(t.strip() for t in texts if t.strip())
        return True
