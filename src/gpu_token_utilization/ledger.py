"""A record of every job: <project>/.gtu/jobs/<job id>/ledger.json.

One entry per step that finished or failed: the step's name, the model it ran on, when it
started and finished (UTC), the tokens it used, the tools the agent reported using (name and
target, as reported; empty for a review step answered by the API) and the step's outcome text.
The file is rewritten as soon as a step ends, so a crash later leaves a usable record of what
ran. The export reads it to write RUN.md next to the job's files.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

FILE = "ledger.json"
VERSION = 1


def path(state_dir: Path) -> Path:
    """The job's ledger file inside its state folder (.gtu/jobs/<id>)."""
    return Path(state_dir) / FILE


def iso(when: datetime | None) -> str | None:
    """UTC ISO 8601 to the second; a naive datetime is taken as UTC already."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def empty() -> dict:
    return {"version": VERSION, "steps": []}


def read(state_dir: Path) -> dict:
    """The ledger as written so far: {"version", "job", "kind", "target", "steps": [...]}.
    An empty one when there is no file yet, or the file can't be read."""
    try:
        doc = json.loads(path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        return empty()
    return doc


def write(state_dir: Path, doc: dict) -> None:
    """Whole file, then rename into place: a crash mid-write leaves the previous version, never
    half a file. If the rename is refused (a program holding the file open, on Windows), the
    file is written in place instead."""
    target = path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(temp, target)
    except OSError:
        target.write_text(text, encoding="utf-8", newline="\n")
        temp.unlink(missing_ok=True)


def record(state_dir: Path, job, *, step: str, status: str, model: str | None, started: datetime | None,
           finished: datetime | None, tokens: int, tools: list[str], outcome: str | None) -> dict:
    """Append one step's entry and write the ledger. `job` needs id, kind and target."""
    doc = read(state_dir)
    doc.update({"version": VERSION, "job": job.id, "kind": job.kind, "target": job.target})
    doc["steps"].append({
        "step": step,
        "status": status,
        "model": model,
        "started": iso(started),
        "finished": iso(finished),
        "tokens": int(tokens or 0),
        "tools": list(tools),
        "outcome": outcome or "",
    })
    write(state_dir, doc)
    return doc


def relative_tools(tools: list[str], root: Path | None) -> list[str]:
    """Tool entries ("<tool> <target>") with a target inside the job's copy shown relative to it,
    with "/" separators: the copy is deleted when the job ends, so its absolute path means
    nothing afterwards. Anything else (a command line, a path elsewhere) is kept as reported."""
    if root is None:
        return [t for t in tools if t]
    prefix = os.path.normcase(str(Path(root)))
    out = []
    for entry in tools:
        if not entry:
            continue
        tool, _, target = entry.partition(" ")
        if target and os.path.normcase(target).startswith(prefix):
            rest = target[len(prefix):].replace("\\", "/").lstrip("/")
            entry = f"{tool} {rest}".strip()
        out.append(entry)
    return out


def total_tokens(doc: dict) -> int:
    return sum(int(s.get("tokens") or 0) for s in doc.get("steps", []))


def last_model(doc: dict) -> str | None:
    """The model of the most recent step that names one."""
    for entry in reversed(doc.get("steps", [])):
        if entry.get("model"):
            return entry["model"]
    return None
