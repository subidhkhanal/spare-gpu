"""Drives BharatCode's own CLI (`bharatcode run`) as the worker for tests and docs jobs.

Safeguards, all enforced here:
- it only ever runs in the job's worktree (`--dir`). Not with `--pure`: that also drops the
  plugin BharatCode signs in with, and every step after the first fails with "Credential store
  permissions or access could not be verified" (checked on 31 Oct, CLI 1.15.46);
- a deny-first opencode.json allows editing plus an allowlist of commands only, and no web
  fetching. BharatCode applies the last matching rule, so "*": "deny" must come first;
- the CLI's skills from ~/.claude/skills and ~/.agents/skills are switched off: it lists every one
  in every request (3.5k tokens a step on the machine measured), and a job never uses them;
- one CLI call per job step, killed if it runs past its timeout;
- if BharatCode fails to read its own sign-in mid-step, the same session is continued
  (`--session`, up to 8 times) instead of failing the job and losing its work;
- tokens are read from each `step_finish` event as it streams in, reported straight away,
  and the CLI is killed as soon as the budget for the step is used up.

Its requests do not go through our client, so there is no x-request-id or queue wait for
them; only what the CLI reports (tokens per step, timings, tool calls) is recorded.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .api import env_without_key

DEFAULT_MODEL = "bharatcode/qwen-3.8-27b"
CMD_ENV = "GTU_BHARATCODE_CMD"
CONFIG_FILE = "opencode.json"

# BharatCode's CLI (1.15.46) sometimes fails this way at the start of a step, reading its own
# sign-in, while the session itself is fine. Seen on 31 Oct even without --pure.
CREDENTIAL_ERROR = "Credential store permissions or access could not be verified"
CREDENTIAL_RETRIES = 8  # each continued process often gets only a step or two done; budget and timeout still cap it
CONTINUE_PROMPT = "Continue the task from where you left off."
# Measured 31 Oct: 16 Claude Code skills cost 3,504 tokens per step, about 30% of the fixed overhead
# (research/token-overhead.md). spare-gpu's own MCP server is switched off in permission_config.
JOB_ENV = {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}


CLI_KINDS = ("tests", "docs")  # job kinds that run the CLI; review talks to the API directly
INSTALL_HELP = (
    "tests and docs jobs need BharatCode's CLI; review jobs don't. To install it:\n"
    "  1. Install Node.js (the LTS version) from https://nodejs.org, then open a new terminal\n"
    "  2. npm install -g bharatcode\n"
    "  3. bharatcode auth login"
)


class CliNotFound(Exception):
    """BharatCode's CLI is not installed or cannot be run safely."""


def resolve_command() -> list[str]:
    """How to start the CLI. GTU_BHARATCODE_CMD overrides (a JSON list or a shell-style string)."""
    override = os.environ.get(CMD_ENV, "").strip()
    if override:
        return json.loads(override) if override.startswith("[") else shlex.split(override)
    found = shutil.which("bharatcode")
    if not found:
        raise CliNotFound(f"BharatCode's CLI isn't installed (no `bharatcode` command found).\n{INSTALL_HELP}")
    if found.lower().endswith((".cmd", ".bat")):
        # The npm shim runs through cmd.exe, which would reinterpret characters in the prompt
        # (& | > and friends). Run the script it wraps with node directly instead.
        script = Path(found).parent / "node_modules" / "bharatcode" / "bin" / "bharatcode.mjs"
        node = shutil.which("node")
        if node and script.exists():
            return [node, str(script)]
        raise CliNotFound(f"found {found} but not the script it wraps; set {CMD_ENV}")
    return [found]


@dataclass
class CliStatus:
    command: list[str] | None
    version: str | None = None
    signed_in: bool | None = None  # None: couldn't tell
    problem: str | None = None  # set when the CLI can't be used at all


def cli_status(timeout: float = 30.0) -> CliStatus:
    """Is the CLI installed, which version, and is it signed in? Runs `--version` and
    `auth status` locally; prints nothing about the account itself."""
    try:
        command = resolve_command()
    except CliNotFound as exc:
        return CliStatus(None, problem=str(exc))

    def run(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env_without_key())
        except (OSError, subprocess.TimeoutExpired):
            return None

    shown = run("--version")
    lines = shown.stdout.strip().splitlines() if shown and shown.returncode == 0 else []
    if not lines:
        return CliStatus(command, problem=f"found `bharatcode` ({command[-1]}) but `bharatcode --version` "
                                          f"didn't run. Reinstall it:\n{INSTALL_HELP}")
    auth = run("auth", "status")
    signed_in = None
    if auth is not None:
        text = (auth.stdout + auth.stderr).lower()  # the CLI prints its status to stderr
        signed_in = auth.returncode == 0 and "signed in" in text and "not signed in" not in text
    return CliStatus(command, version=lines[-1].strip(), signed_in=signed_in)


def permission_config(edit: bool, commands: tuple[str, ...]) -> dict:
    """opencode.json allowing edits (optionally) and only the listed commands."""
    bash: dict | str = "deny"
    if commands:
        bash = {"*": "deny"}  # deny first: the last matching rule wins
        for command in commands:
            bash[f"{command}*"] = "allow"
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"edit": "allow" if edit else "deny", "bash": bash, "webfetch": "deny"},
        # spare-gpu's own MCP server is off inside jobs: its tool schemas cost ~2.3k tokens on
        # every step (measured), and a job's agent has no business queueing more jobs.
        "mcp": {"spare-gpu": {"type": "local", "command": ["gtu", "mcp"], "enabled": False}},
    }


@dataclass
class CliStep:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    total_tokens: int
    elapsed_ms: float | None


@dataclass
class CliResult:
    exit_code: int | None
    text: str
    steps: list[CliStep] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    stopped: str | None = None  # "timeout", "budget" or "stop" when we killed it
    retries: int = 0  # times the session was continued after BharatCode's credential error

    @property
    def tokens(self) -> int:
        return sum(s.total_tokens for s in self.steps)

    @property
    def ok(self) -> bool:
        return self.stopped is None and self.exit_code == 0 and self.error is None


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _pump(stream, sink: queue.Queue) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)


class BharatCodeCli:
    def __init__(self, command: list[str] | None = None, model: str = DEFAULT_MODEL, timeout: float = 900.0):
        self.command = command
        self.model = model
        self.timeout = timeout

    def run(
        self,
        workdir: Path,
        prompt: str,
        *,
        edit: bool = True,
        commands: tuple[str, ...] = (),
        token_budget: int | None = None,
        on_step: Callable[[CliStep], None] = lambda step: None,
        should_stop: Callable[[], bool] = lambda: False,
        on_retry: Callable[[int], None] = lambda attempt: None,
    ) -> CliResult:
        workdir = Path(workdir)
        command = self.command or resolve_command()
        config = workdir / CONFIG_FILE
        config.write_text(json.dumps(permission_config(edit, commands), indent=2), encoding="utf-8")
        result = CliResult(exit_code=None, text="")
        texts: list[str] = []
        deadline = time.monotonic() + self.timeout
        try:
            session = self._attempt(command, workdir, prompt, None, result, texts, deadline, token_budget,
                                    on_step, should_stop)
            # BharatCode sometimes fails to read its own sign-in at the start of a step. The work so
            # far is in its session, so carry on there instead of failing the job.
            while (result.retries < CREDENTIAL_RETRIES and session and not result.stopped
                   and CREDENTIAL_ERROR in (result.error or "")):
                result.retries += 1
                result.error = None
                on_retry(result.retries)
                session = self._attempt(command, workdir, CONTINUE_PROMPT, session, result, texts, deadline,
                                        token_budget, on_step, should_stop) or session
        finally:
            config.unlink(missing_ok=True)
        result.text = "\n".join(t for t in texts if t).strip()
        return result

    def _attempt(self, command, workdir, message, session, result, texts, deadline, token_budget,
                 on_step, should_stop) -> str | None:
        """One `bharatcode run`, adding to `result`. Returns the CLI session id it reported."""
        args = [*command, "run", "--dir", str(workdir), "-m", self.model, "--format", "json"]
        if session:
            args += ["--session", session]
        args.append(message)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            args, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env={**env_without_key(), **JOB_ENV}, **kwargs,
        )
        lines: queue.Queue = queue.Queue()
        stderr: list[str] = []
        threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()

        seen_session: str | None = None
        step_started: float | None = None
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    result.stopped = "timeout"
                    break
                if should_stop():
                    result.stopped = "stop"  # `gtu stop`
                    break
                try:
                    line = lines.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind, part = event.get("type"), event.get("part") or {}
                seen_session = event.get("sessionID") or part.get("sessionID") or seen_session
                if kind == "step_start":
                    step_started = event.get("timestamp")
                elif kind == "step_finish":
                    tokens = part.get("tokens") or {}
                    finished = event.get("timestamp")
                    step = CliStep(
                        input_tokens=tokens.get("input") or 0,
                        output_tokens=tokens.get("output") or 0,
                        reasoning_tokens=tokens.get("reasoning") or 0,
                        cache_read_tokens=(tokens.get("cache") or {}).get("read") or 0,
                        total_tokens=tokens.get("total") or 0,
                        elapsed_ms=(finished - step_started) if finished and step_started else None,
                    )
                    result.steps.append(step)
                    on_step(step)
                    if token_budget is not None and result.tokens >= token_budget:
                        result.stopped = "budget"
                        break
                elif kind == "tool_use":
                    state = part.get("state") or {}
                    target = (state.get("input") or {}).get("command") or (state.get("input") or {}).get("filePath") or ""
                    result.tools.append(f"{part.get('tool')} {target}".strip())
                elif kind == "text":
                    texts.append(part.get("text") or "")
                elif kind == "error":
                    data = (event.get("error") or {}).get("data") or {}
                    result.error = data.get("message") or json.dumps(event.get("error"))[:300]
        finally:
            if result.stopped:
                _kill_tree(proc)
            try:
                result.exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                result.exit_code = proc.wait()
        if result.exit_code not in (0, None) and not result.error and not result.stopped:
            result.error = ("".join(stderr).strip() or f"exited with code {result.exit_code}")[:300]
        return seen_session
