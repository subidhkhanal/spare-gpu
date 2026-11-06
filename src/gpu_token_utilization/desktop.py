"""Runs job steps through BharatCode Desktop's own local server, instead of the `bharatcode` CLI.

Desktop runs BharatCode's server (the OpenCode HTTP API) on 127.0.0.1 with a password made for
each launch, and starts MCP servers such as ours as its child processes. They inherit
OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD, but not the port, so the port is found as
the one a parent process listens on (or given in GTU_DESKTOP_URL, which the MCP server sets for
the background runner it starts). A job step is then one Desktop session:
- created in the job's worktree (`?directory=`), so it shows up in Desktop like any other chat;
- with the same deny-first permission file the CLI runner used, written into the worktree;
- with the model chosen per task (the router's "sticky per task" rule);
- polled for its steps, so tokens are reported as they come in and the token budget, the
  timeout and `gtu stop` all abort the session.
It uses Desktop's sign-in, so the CLI's credential-store bug and its separate login are gone.
Jobs only run while Desktop is open: the server lives inside it.
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

from .agent_cli import CONFIG_FILE, DEFAULT_MODEL, CliResult, CliStep, permission_config

URL_ENV = "GTU_DESKTOP_URL"
USER_ENV = "OPENCODE_SERVER_USERNAME"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
HEALTH = "/global/health"
# runner -> its launcher -> MCP server -> its launcher -> Desktop's server process -> Desktop
ANCESTORS = 6
WEAK_CANDIDATES = 8  # ports with little evidence of being a server, tried after the likely ones
POLL_RETRIES = 5
STALL_POLLS = 15  # polls a session may sit idle with an unfinished reply before the step gives up  # dropped connections in a row while following a session before giving up


DESKTOP_NEEDED = ("Tests and docs jobs run inside BharatCode Desktop: open this project in Desktop and ask "
                  "spare-gpu there (or run `gtu start` from Desktop's terminal).")


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


def export_url(conn: Connection) -> None:
    """Pass the found server on to processes we start (the background runner can't see Desktop)."""
    os.environ[URL_ENV] = conn.url


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


def _model_ref(model: str) -> dict:
    provider, _, model_id = model.partition("/")
    return {"providerID": provider, "modelID": model_id} if model_id else {"providerID": "bharatcode", "modelID": provider}


def _error_text(error: dict) -> str:
    return (error.get("data") or {}).get("message") or error.get("name") or json.dumps(error)[:300]


class DesktopAgent:
    """Same interface as agent_cli.BharatCodeCli.run, backed by a Desktop session."""

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
            token_budget: int | None = None, on_step: Callable[[CliStep], None] = lambda step: None,
            should_stop: Callable[[], bool] = lambda: False,
            on_retry: Callable[[int], None] = lambda attempt: None) -> CliResult:
        workdir = Path(workdir)
        config = workdir / CONFIG_FILE
        config.write_text(json.dumps(permission_config(edit, commands, self.mcp_servers), indent=2), encoding="utf-8")
        result = CliResult(exit_code=None, text="")
        where = {"directory": str(workdir)}
        try:
            with self.conn.client(transport=self._transport) as http:
                created = http.post("/session", params=where, json={
                    "title": f"spare-gpu: {prompt.splitlines()[0][:60]}",
                    "permission": session_rules(edit, commands),
                })
                created.raise_for_status()
                session = created.json()["id"]
                sent = http.post(f"/session/{session}/prompt_async", params=where,
                                 json={"model": _model_ref(self.model), "parts": [{"type": "text", "text": prompt}]})
                sent.raise_for_status()
                self._follow(http, session, where, result, token_budget, on_step, should_stop)
        except httpx.HTTPError as exc:
            result.error = f"BharatCode Desktop's server: {exc}"
        finally:
            config.unlink(missing_ok=True)
        if result.exit_code is None:
            result.exit_code = 1 if result.error or result.stopped else 0
        return result

    def ask(self, workdir: Path, messages: list[dict], *, token_budget: int | None = None,
            on_step: Callable[[CliStep], None] = lambda step: None,
            should_stop: Callable[[], bool] = lambda: False) -> CliResult:
        """A one-answer request (the review job) as a Desktop session that may read but not edit
        or run anything, so it needs Desktop's sign-in rather than spare-gpu's own API key."""
        prompt = "\n\n".join(m["content"] for m in messages if m.get("content"))
        prompt += "\n\nReply with the report itself and nothing else. Don't use any tools."
        return self.run(workdir, prompt, edit=False, commands=(), token_budget=token_budget, on_step=on_step,
                        should_stop=should_stop)

    def _follow(self, http, session, where, result, token_budget, on_step, should_stop) -> None:
        deadline = self._clock() + self.timeout
        seen: set[str] = set()
        dropped = idle_unfinished = 0
        while True:
            self._sleep(self.poll * (2 ** dropped))
            try:
                messages = http.get(f"/session/{session}/message", params=where)
                messages.raise_for_status()
                listed = messages.json()
                finished = self._read(listed, seen, result, on_step)
                # Each step is its own assistant message, so a completed one doesn't mean the agent
                # is done; the session's status does. Idle sessions are left out of the status map.
                busy = session in self._statuses(http, where)
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
                self._abort(http, session, where)
                return
            if done:
                return

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
    def _read(messages: list[dict], seen: set[str], result: CliResult, on_step) -> bool:
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
                    step = CliStep(
                        input_tokens=tokens.get("input") or 0, output_tokens=tokens.get("output") or 0,
                        reasoning_tokens=tokens.get("reasoning") or 0, cache_read_tokens=cache.get("read") or 0,
                        total_tokens=tokens.get("total") or sum(
                            [tokens.get("input") or 0, tokens.get("output") or 0, tokens.get("reasoning") or 0,
                             cache.get("read") or 0, cache.get("write") or 0]),
                        elapsed_ms=None,
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
        result.text = texts[-1].strip() if texts else ""
        return True
