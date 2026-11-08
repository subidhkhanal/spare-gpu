"""Measure BharatCode Desktop's fixed token overhead: a trivial prompt, one session, in different
folders.

Each run is one Desktop session on Desktop's default agent; the step-finish part of its reply
reports the tokens it sent (input, cache read/write) and got back (output, reasoning). The context
it sent before the model saw our handful of words is the overhead. Run it from BharatCode
Desktop's terminal (it talks to Desktop's own server).

Usage (one run per variant, a few thousand to ~15k tokens each):
    python scripts/measure_overhead.py <project-folder> <empty-folder> [variant ...]
Variants: empty, empty-mcp, project, agents-md. Prints one JSON line per run.
agents-md needs a folder whose only content is an AGENTS.md of known size: pass it as GTU_AGENTS_MD_DIR.
A variant that switches spare-gpu's MCP server off writes an opencode.json into its folder for the
run and removes it after, so give it a folder of its own (never a project you work in).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from gpu_token_utilization import desktop

PROMPT = "Reply with the single word OK."
MCP_OFF = {"$schema": "https://opencode.ai/config.json",
           "mcp": {"spare-gpu": {"type": "local", "command": ["gtu", "mcp"], "enabled": False}}}


def run(agent: desktop.DesktopAgent, folder: Path, config: dict | None) -> dict:
    written = folder / "opencode.json"
    if config is not None:
        if written.exists():
            raise SystemExit(f"{written} exists already; use a folder of its own for this variant")
        written.write_text(json.dumps(config), encoding="utf-8")
    started = time.monotonic()
    try:
        result = agent.run_agent(folder, None, PROMPT)
    finally:
        if config is not None:
            written.unlink(missing_ok=True)
    steps = [{"input": s.input_tokens, "output": s.output_tokens, "reasoning": s.reasoning_tokens,
              "cache_read": s.cache_read_tokens, "cache_write": s.cache_write_tokens, "total": s.total_tokens}
             for s in result.steps]
    return {"steps": steps, "reply": result.text.strip()[:40], "error": result.error, "exit": result.exit_code,
            "seconds": round(time.monotonic() - started, 1)}


def main(project: str, empty: str, variants: list[str]) -> None:
    plans = {
        "empty": (Path(empty), MCP_OFF),       # the harness alone: system prompt + built-in tools
        "empty-mcp": (Path(empty), None),      # plus spare-gpu's MCP tool schemas (as registered in Desktop)
        "project": (Path(project), None),      # plus project context (file tree, git, AGENTS.md...); its config untouched
        "agents-md": (Path(os.environ.get("GTU_AGENTS_MD_DIR", empty)), MCP_OFF),  # plus one AGENTS.md
    }
    agent = desktop.DesktopAgent(desktop.discover(), timeout=300)
    for name in variants or [v for v in plans if v != "agents-md"]:
        folder, config = plans[name]
        print(json.dumps({"variant": name, **run(agent, folder, config)}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
