"""What every job step shares, whoever runs it: the permission file a job's session runs under,
and the steps and result the session reports. desktop.DesktopAgent runs the session.

Safeguards, all set here:
- a deny-first opencode.json allows reading, editing when the job writes, and an allowlist of
  commands only, and no web fetching. BharatCode applies the last matching rule, so "*": "deny"
  must come first;
- the user's MCP servers and spare-gpu's own are switched off inside a job;
- nothing may "ask": nobody watches a job's session, so a prompt there would wait forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .bc_config import user_mcp_servers

DEFAULT_MODEL = "bharatcode/qwen-3.8-27b"
CONFIG_FILE = "opencode.json"
AGENT_KINDS = ("tests", "docs", "fix")  # job kinds whose steps run as an agent session; review is one answer


def job_mcp_servers(given: Mapping[str, dict] | None) -> dict[str, dict]:
    """The user's MCP servers a job must switch off: as given, else read from their BharatCode config."""
    return dict(given) if given is not None else user_mcp_servers()


def _switched_off(entry: dict) -> dict:
    """A user's MCP entry as the job's config repeats it: only what BharatCode needs to accept the
    entry (its type and how to reach it), never its environment or headers, and switched off."""
    kept = {key: entry[key] for key in ("type", "command", "url") if key in entry}
    return {**kept, "enabled": False}


def permission_config(edit: bool, commands: tuple[str, ...], mcp_servers: Mapping[str, dict] | None = None) -> dict:
    """opencode.json for a job: everything denied first, then only what the job needs (reading and
    searching its copy, editing when it writes, and the listed commands), with the user's MCP
    servers switched off. Written into the job's private copy, so it applies to that session
    whatever the user's own config allows. `mcp_servers` None means: read the user's config."""
    bash: dict | str = "deny"
    if commands:
        bash = {"*": "deny"}  # deny first: the last matching rule wins
        for command in commands:
            bash[f"{command}*"] = "allow"
    permission = {
        # Everything is denied first. BharatCode keeps the file's key order and the last matching
        # rule wins, so only what's re-allowed below is on; a tool whose last rule is a plain deny
        # is also left out of the model's tool list, MCP tools included (permission/index.ts).
        "*": "deny",
        "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
        "glob": "allow", "grep": "allow", "list": "allow",
        "edit": "allow" if edit else "deny",
        "bash": bash,
        # Spelled out although "*" already covers them: "skill": "deny" also drops the list of the
        # user's skills from every request (session/system.ts), ~3.5k tokens a step on the machine
        # measured. Nobody watches a job's session, so nothing may "ask": a prompt there waits
        # forever (seen 5 Nov: a docs job sat for 10+ minutes on a prompt to read the main repo's
        # .git/config). Each of these is "ask" in Desktop's defaults.
        "webfetch": "deny", "skill": "deny", "external_directory": "deny", "doom_loop": "deny", "question": "deny",
    }
    # The user's MCP servers are off inside jobs (not started, and their tools hidden by "*"): a
    # job has no business reaching them. spare-gpu's own server too: its tool schemas cost tokens
    # on every step, and a job's agent mustn't queue more jobs.
    mcp = {name: _switched_off(entry) for name, entry in job_mcp_servers(mcp_servers).items()}
    mcp["spare-gpu"] = {"type": "local", "command": ["gtu", "mcp"], "enabled": False}
    return {"$schema": "https://opencode.ai/config.json", "permission": permission, "mcp": mcp}


@dataclass
class AgentStep:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    total_tokens: int
    elapsed_ms: float | None
    cache_write_tokens: int = 0


@dataclass
class AgentResult:
    exit_code: int | None
    text: str
    steps: list[AgentStep] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    stopped: str | None = None  # "timeout", "budget" or "stop" when we ended it

    @property
    def tokens(self) -> int:
        return sum(s.total_tokens for s in self.steps)

    @property
    def ok(self) -> bool:
        return self.stopped is None and self.exit_code == 0 and self.error is None
