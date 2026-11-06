"""Reading `bharatcode stats --models`: the per-model token figures behind the pack's pack_cost
tool and the benchmark scripts. BharatCode prints the counts rounded to 0.1K (or 0.1M), so a
delta between two readings is good to about a hundred tokens per model."""

from __future__ import annotations

import re
import subprocess

from .agent_cli import CliNotFound, resolve_command
from .api import env_without_key


def usage(days: int = 7, command: list[str] | None = None, timeout: float = 60.0) -> str | None:
    """`bharatcode stats --days N --models`, as text; None when the CLI isn't there or has no stats."""
    try:
        argv = command or resolve_command()
    except CliNotFound:
        return None
    try:
        proc = subprocess.run([*argv, "stats", "--days", str(days), "--models"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                              env=env_without_key(NO_COLOR="1", TERM="dumb"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", proc.stdout).strip()  # every CSI sequence: colours, erase-line, cursor
    return text or None


MODEL_LINE = re.compile(r"[A-Za-z][\w.-]*/[\w.:+-]+")
STATS_ROW = re.compile(r"^\s*(Messages|Input Tokens|Output Tokens|Cache Read|Cache Write|Cost)\s+(\S+)\s*$")


def _stats_number(value: str) -> float:
    value = value.replace(",", "").lstrip("$")
    scale = {"K": 1_000, "M": 1_000_000}.get(value[-1:].upper(), 1)
    try:
        return float(value.rstrip("KkMm")) * scale
    except ValueError:
        return 0.0


def parse_stats(text: str) -> dict[str, dict[str, float]]:
    """The MODEL USAGE section of `bharatcode stats --models`, by model: messages, input, output,
    cache_read, cache_write and tokens (their sum). BharatCode prints the counts rounded to 0.1K
    (or 0.1M), so a delta between two readings is good to about a hundred tokens per model."""
    models: dict[str, dict[str, float]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip().strip("│").strip()
        if not line or set(line) <= set("─┌┐└┘├┤ "):
            continue
        if "TOOL USAGE" in line:
            break  # the tool box comes after the models; nothing in it is a model
        row = STATS_ROW.match(line)
        if row and current is not None:
            key = {"Messages": "messages", "Input Tokens": "input", "Output Tokens": "output",
                   "Cache Read": "cache_read", "Cache Write": "cache_write", "Cost": "cost"}[row.group(1)]
            models[current][key] = _stats_number(row.group(2))
        elif MODEL_LINE.fullmatch(line):  # provider/model, as the box prints it; not a date or a path
            current = line
            models[current] = {"messages": 0.0, "input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cost": 0.0}
    for figures in models.values():
        figures["tokens"] = figures["input"] + figures["output"] + figures["cache_read"] + figures["cache_write"]
    return models


def model_usage(days: int = 1, command: list[str] | None = None) -> dict[str, dict[str, float]] | None:
    text = usage(days, command=command)
    return None if text is None else parse_stats(text)
