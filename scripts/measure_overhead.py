"""Measure BharatCode CLI's fixed token overhead: a trivial prompt, one step, in different settings.

Each run is `bharatcode run --format json "<tiny prompt>"`; the CLI's own step_finish event reports
the tokens it sent (input, cache read/write) and got back (output, reasoning). The context it
sent before the model saw our handful of words is the overhead.

Usage (one run per variant, a few thousand to ~15k tokens each):
    python scripts/measure_overhead.py <project-folder> <empty-folder> [variant ...]
Variants: empty, empty-mcp, project, empty-notools, empty-noskills, agents-md. Prints one JSON line per run.
agents-md needs a folder whose only content is an AGENTS.md of known size: pass it as GTU_AGENTS_MD_DIR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gpu_token_utilization.agent_cli import resolve_command
from gpu_token_utilization.api import env_without_key

PROMPT = "Reply with the single word OK."
MCP_OFF = {"mcp": {"spare-gpu": {"type": "local", "command": ["gtu", "mcp"], "enabled": False}}}
TOOLS_OFF = {**MCP_OFF, "tools": {"*": False}}


def run(folder: Path, extra_config: dict | None, extra_env: dict | None = None) -> dict:
    env = {**env_without_key(), **(extra_env or {})}
    config_file = None
    if extra_config is not None:
        handle, name = tempfile.mkstemp(suffix=".json")
        os.close(handle)  # Windows can't delete a file that's still open
        config_file = Path(name)
        config_file.write_text(json.dumps(extra_config), encoding="utf-8")
        env["OPENCODE_CONFIG"] = str(config_file)
    started = time.monotonic()
    proc = subprocess.run([*resolve_command(), "run", "--format", "json", PROMPT], cwd=folder, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                          stdin=subprocess.DEVNULL)
    if config_file:
        config_file.unlink(missing_ok=True)
    steps, text, error = [], "", None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "step_finish":
            t = event["part"]["tokens"]
            steps.append({"input": t.get("input", 0), "output": t.get("output", 0),
                          "reasoning": t.get("reasoning", 0), "cache_read": t.get("cache", {}).get("read", 0),
                          "cache_write": t.get("cache", {}).get("write", 0), "total": t.get("total")})
        elif event.get("type") == "text":
            text += event["part"].get("text", "")
        elif event.get("type") == "error":
            error = event.get("error", {}).get("data", {}).get("message")
    return {"steps": steps, "reply": text.strip()[:40], "error": error, "exit": proc.returncode,
            "seconds": round(time.monotonic() - started, 1)}


def main(project: str, empty: str, variants: list[str]) -> None:
    plans = {
        "empty": (Path(empty), MCP_OFF),          # the harness alone: system prompt + built-in tools
        "empty-mcp": (Path(empty), None),         # plus spare-gpu's MCP tool schemas (global config; 5 tools since Nov)
        "project": (Path(project), MCP_OFF),      # plus project context (file tree, git, AGENTS.md...)
        "empty-notools": (Path(empty), TOOLS_OFF),  # system prompt without tool schemas, if honoured
        # without the skills it discovers in ~/.claude/skills and ~/.agents/skills
        "empty-noskills": (Path(empty), MCP_OFF, {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}),
        "agents-md": (Path(os.environ.get("GTU_AGENTS_MD_DIR", empty)), MCP_OFF),  # plus one AGENTS.md
    }
    for name in variants or [v for v in plans if v != "agents-md"]:
        folder, config, *env = plans[name]
        result = run(folder, config, *env)
        print(json.dumps({"variant": name, **result}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
