"""The settings `scripts/improve.py` may tune (tuning.json, next to this file): the thresholds
verify_refs judges references by, and the figures /auto's routing decides with. Each module reads
its values from here when it is imported; the improvement loop tries other values against a fixed
benchmark and writes them back here only when they win and the person running it promotes them.
A missing or unreadable file means the defaults in the code.
"""

from __future__ import annotations

import json
from pathlib import Path

TUNING_FILE = Path(__file__).with_name("tuning.json")


def load(path: Path = TUNING_FILE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def value(section: str, name: str, default):
    """tuning.json's [section][name], as the default's type; the default when it's missing or wrong."""
    found = (load().get(section) or {}).get(name, default)
    try:
        return type(default)(found)
    except (TypeError, ValueError):
        return default
