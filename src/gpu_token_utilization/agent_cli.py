"""Drives BharatCode's own CLI (`bharatcode run`) as the worker for tests and docs jobs.

Safeguards, all enforced here:
- it only ever runs in the job's worktree (`--dir`), with `--pure` (no global plugins);
- a deny-first opencode.json allows editing plus an allowlist of commands only, and no web
  fetching. BharatCode applies the last matching rule, so "*": "deny" must come first;
- one CLI call per job step, killed if it runs past its timeout;
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

DEFAULT_MODEL = "bharatcode/qwen-3.8-27b"
CMD_ENV = "GTU_BHARATCODE_CMD"
CONFIG_FILE = "opencode.json"


class CliNotFound(Exception):
    """BharatCode's CLI is not installed or cannot be run safely."""


def resolve_command() -> list[str]:
    """How to start the CLI. GTU_BHARATCODE_CMD overrides (a JSON list or a shell-style string)."""
    override = os.environ.get(CMD_ENV, "").strip()
    if override:
        return json.loads(override) if override.startswith("[") else shlex.split(override)
    found = shutil.which("bharatcode")
    if not found:
        raise CliNotFound("BharatCode CLI not found; install it with `npm install -g bharatcode`")
    if found.lower().endswith((".cmd", ".bat")):
        # The npm shim runs through cmd.exe, which would reinterpret characters in the prompt
        # (& | > and friends). Run the script it wraps with node directly instead.
        script = Path(found).parent / "node_modules" / "bharatcode" / "bin" / "bharatcode.mjs"
        node = shutil.which("node")
        if node and script.exists():
            return [node, str(script)]
        raise CliNotFound(f"found {found} but not the script it wraps; set {CMD_ENV}")
    return [found]


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
    stopped: str | None = None  # "timeout" or "budget" when we killed it

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
    ) -> CliResult:
        workdir = Path(workdir)
        command = self.command or resolve_command()
        config = workdir / CONFIG_FILE
        config.write_text(json.dumps(permission_config(edit, commands), indent=2), encoding="utf-8")
        args = [*command, "run", "--pure", "--dir", str(workdir), "-m", self.model, "--format", "json", prompt]
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            args, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **kwargs,
        )
        lines: queue.Queue = queue.Queue()
        stderr: list[str] = []
        threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()

        result = CliResult(exit_code=None, text="")
        texts: list[str] = []
        step_started: float | None = None
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    result.stopped = "timeout"
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
            config.unlink(missing_ok=True)
        result.text = "\n".join(t for t in texts if t).strip()
        if result.exit_code not in (0, None) and not result.error and not result.stopped:
            result.error = ("".join(stderr).strip() or f"exited with code {result.exit_code}")[:300]
        return result
