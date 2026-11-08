"""`tally_findings`: the sanity check between the finders and the verifier of /code-review and
/security-review.

Finders on the standard model return JSON lists of candidates. Before the heavy verifier spends
tokens on them, this merges what several finders agree on, drops entries that aren't findings
(no file, no line, no claim) and flags text that reads like an instruction to the lead rather
than a finding: a page or a comment the finder quoted may be trying to steer the review. It
judges nothing about the code itself; that is the verifier's job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Phrases that address the reader instead of describing the code. Any one of them marks the
# candidate suspect; the lead drops suspects without reading them as findings.
STEERING = re.compile(
    r"(ignore (all |the )?(previous|prior|above|earlier) (instructions|rules|guidance)|"
    r"disregard (the|your|all) (previous|prior|above)|as the (lead|verifier|reviewer),? you (must|should|will)|"
    r"you must (now|immediately)|new instructions?:|system prompt|do not (report|mention) this|"
    r"mark (this|it) as (safe|clean|verified)|approve (this|the) (change|finding)|<\|[a-z_]+\|>|"
    r"\btool_call\b|\bfunction_call\b)",
    re.I,
)
TEXT_KEYS = ("claim", "description", "failure_scenario", "scenario", "exploit_scenario", "recommendation", "category")
MAX_TEXT = 1_200  # a finding is a sentence or two; a wall of text is a quote, not a finding


@dataclass
class Tally:
    kept: list[dict] = field(default_factory=list)
    invalid: list[tuple[dict, str]] = field(default_factory=list)
    suspect: list[tuple[dict, str]] = field(default_factory=list)
    total: int = 0


def parse(text: str) -> list[dict]:
    """The candidates as the finders returned them: one JSON list, or several lists one after the
    other (the lead pastes each finder's answer), or an object with a findings/candidates list.
    Markdown code fences are tolerated."""
    body = text.strip()
    chunks: list[str] = []
    if "```" in body:
        for part in body.split("```")[1::2]:  # the fenced blocks
            chunks.append(part.split("\n", 1)[1] if "\n" in part and part.split("\n", 1)[0].strip().isalpha() else part)
    else:
        chunks.append(body)
    out: list[dict] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        decoder = json.JSONDecoder()
        at = 0
        while at < len(chunk):
            while at < len(chunk) and chunk[at] not in "[{":
                at += 1
            if at >= len(chunk):
                break
            data, end = decoder.raw_decode(chunk, at)
            at = end
            if isinstance(data, dict):
                data = data.get("findings", data.get("candidates", [data]))
            if not isinstance(data, list):
                raise ValueError("expected JSON lists of candidates (objects with file, line and a claim)")
            out += [d for d in data if isinstance(d, dict)]
    if not out and body:
        raise ValueError("expected JSON lists of candidates (objects with file, line and a claim)")
    return out


def _claim(candidate: dict) -> str:
    return str(candidate.get("claim") or candidate.get("description") or "").strip()


def _invalid_reason(candidate: dict) -> str | None:
    file = candidate.get("file")
    if not isinstance(file, str) or not file.strip():
        return "no file"
    line = candidate.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        try:
            if not (isinstance(line, str) and int(line) >= 1):
                return "no line number"
        except ValueError:
            return "no line number"
    if not _claim(candidate):
        return "no claim or description"
    confidence = candidate.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and not 0 <= confidence <= 1:
        return "confidence outside 0 to 1"
    return None


def _suspect_reason(candidate: dict) -> str | None:
    for key in TEXT_KEYS:
        value = candidate.get(key)
        if not isinstance(value, str):
            continue
        if len(value) > MAX_TEXT:
            return f"{key} is {len(value)} characters: a quoted block, not a finding"
        m = STEERING.search(value)
        if m:
            return f"{key} addresses the reader (\"{m.group(0)}\")"
    return None


def _key(candidate: dict) -> tuple[str, int]:
    return candidate["file"].strip().replace("\\", "/").lstrip("./"), int(candidate["line"])


CONFIDENCE_WORDS = {"high": 0.9, "medium": 0.6, "low": 0.3}


def _confidence(candidate: dict) -> float | None:
    value = candidate.get("confidence")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return CONFIDENCE_WORDS.get(value.strip().lower())
    return None


def tally(candidates: list[dict]) -> Tally:
    """Validate, flag, and merge by file and line. Kept candidates carry `agreement` (how many
    finders reported that place) and keep the longest claim and the highest confidence."""
    result = Tally(total=len(candidates))
    merged: dict[tuple[str, int], dict] = {}
    for candidate in candidates:
        why = _invalid_reason(candidate)
        if why:
            result.invalid.append((candidate, why))
            continue
        why = _suspect_reason(candidate)
        if why:
            result.suspect.append((candidate, why))
            continue
        key = _key(candidate)
        entry = merged.get(key)
        if entry is None:
            merged[key] = {**candidate, "file": key[0], "line": key[1], "agreement": 1}
            continue
        entry["agreement"] += 1
        if len(_claim(candidate)) > len(_claim(entry)):
            for name in ("claim", "description", "failure_scenario", "scenario", "exploit_scenario", "recommendation"):
                if candidate.get(name):
                    entry[name] = candidate[name]
        a, b = _confidence(entry), _confidence(candidate)
        if b is not None and (a is None or b > a):
            entry["confidence"] = candidate["confidence"]
    result.kept = sorted(merged.values(), key=lambda c: (-c["agreement"], -(_confidence(c) or 0), c["file"], c["line"]))
    return result


def describe(result: Tally) -> str:
    lines = [f"Kept {len(result.kept)} place(s) from {result.total} candidate(s): {len(result.invalid)} not a finding, "
             f"{len(result.suspect)} suspect (addressed to the reader; drop them unread)."]
    for candidate, why in result.invalid:
        lines.append(f"- NOT A FINDING {candidate.get('file', '?')}:{candidate.get('line', '?')}: {why}")
    for candidate, why in result.suspect:
        lines.append(f"- SUSPECT {candidate.get('file', '?')}:{candidate.get('line', '?')}: {why}")
    agreed = [c for c in result.kept if c["agreement"] > 1]
    if agreed:
        lines.append("Reported by more than one finder: " + ", ".join(f"{c['file']}:{c['line']} ({c['agreement']})" for c in agreed))
    lines.append("Kept candidates for the verifier (JSON, most agreed first):")
    lines.append(json.dumps(result.kept, indent=1))
    return "\n".join(lines)
