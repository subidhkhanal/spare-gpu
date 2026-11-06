"""Stand-in for `bharatcode run --format json`, for tests.

A real process that prints the same JSON events and edits files in --dir, scripted by a
JSON scenario file named in GTU_FAKE_CLI_SCENARIO:

  {"steps": [{"tokens": 1000, "writes": {"tests/test_x.py": "..."}, "sleep": 0}],
   "error": null, "exit": 0, "text": "done", "record": "<file to append each call's args and config to>",
   "fail_first": 0}

With "fail_first": n, the first n calls end with "error" and the later ones succeed, like
BharatCode's intermittent credential error followed by a continued session (needs "record").

Run it as: python -m gpu_token_utilization.fakecli run --dir <dir> ... <prompt>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main(args: list[str]) -> int:
    if args == ["--version"]:
        print("0.0.0-fake")
        return 0
    if args[:2] == ["mcp", "list"]:  # connected if the config BharatCode was pointed at registers us
        config = os.environ.get("OPENCODE_CONFIG", "")
        if config and Path(config).exists() and '"spare-gpu"' in Path(config).read_text(encoding="utf-8"):
            print("\x1b[32m✓\x1b[0m spare-gpu connected")
        else:
            print("No MCP servers configured")
        return 0
    if args[:2] == ["auth", "status"]:  # GTU_FAKE_CLI_SIGNED_IN=0 simulates a signed-out CLI
        if os.environ.get("GTU_FAKE_CLI_SIGNED_IN", "1") == "1":
            print("Signed in to BharatCode.")
            return 0
        print("Not signed in. Run `bharatcode auth login`.")
        return 1
    workdir = Path(args[args.index("--dir") + 1])
    scenario = json.loads(Path(os.environ["GTU_FAKE_CLI_SCENARIO"]).read_text(encoding="utf-8"))
    if scenario.get("record"):
        config = workdir / "opencode.json"
        entry = {"args": args, "config": json.loads(config.read_text()) if config.exists() else None,
                 "skills_off": os.environ.get("OPENCODE_DISABLE_EXTERNAL_SKILLS") == "1",
                 "config_env": {k: os.environ.get(k) for k in
                                ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION")}}
        with open(scenario["record"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    session = "ses_fake"
    calls = len(Path(scenario["record"]).read_text(encoding="utf-8").splitlines()) if scenario.get("record") else 1
    failing = bool(scenario.get("error")) and (not scenario.get("fail_first") or calls <= scenario["fail_first"])
    for step in scenario.get("steps", [{"tokens": 1000}]):
        emit({"type": "step_start", "timestamp": int(time.time() * 1000), "sessionID": session, "part": {}})
        for path, content in (step.get("writes") or {}).items():
            target = workdir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            # exactly as given, like a real editor tool: no \n -> \r\n translation on Windows
            target.write_text(content, encoding="utf-8", newline="")
            emit({"type": "tool_use", "sessionID": session,
                  "part": {"tool": "edit", "state": {"status": "completed", "input": {"filePath": str(target)}}}})
        time.sleep(step.get("sleep", 0))
        total = step.get("tokens", 1000)
        emit({"type": "step_finish", "timestamp": int(time.time() * 1000), "sessionID": session,
              "part": {"reason": "tool-calls", "tokens": {"total": total, "input": total - 50, "output": 50,
                                                          "reasoning": 0, "cache": {"read": 0, "write": 0}}}})
    if failing:
        emit({"type": "error", "sessionID": session, "error": {"name": "UnknownError", "data": {"message": scenario["error"]}}})
        return scenario.get("exit", 1)
    emit({"type": "text", "sessionID": session, "part": {"text": scenario.get("text", "done")}})
    return scenario.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
