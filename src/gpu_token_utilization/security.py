"""The deterministic false-positive filter of /security-review.

Follows the method of anthropics/claude-code-security-review (MIT, Copyright (c) 2025 Anthropic;
see pack/ATTRIBUTION.md and pack/licenses/): candidate findings that match known low-signal
patterns are dropped by rule, before any model judges them, so the verifier's time (on the heavy
model) goes to candidates that can be real. The rules are re-implemented here; the categories
and precedents the agents use come from the same source.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
C_LIKE = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")
MIN_CONFIDENCE = 0.7  # below this a finding is too speculative to be worth a verifier's time
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)/|(^|/)(test_[^/]*|[^/]*_test|[^/]*\.test|[^/]*\.spec)\.[a-z]+$", re.I)

# Each rule: (reason, patterns over the finding's text). The first match wins.
RULES: list[tuple[str, tuple[re.Pattern, ...]]] = [
    ("denial of service or resource exhaustion (low signal)", (
        re.compile(r"\b(denial of service|dos attack|resource exhaustion)\b", re.I),
        re.compile(r"\b(exhaust|overwhelm|overload)\w*\b[^.]*\b(resource|memory|cpu)s?\b", re.I),
        re.compile(r"\b(infinite|unbounded)\b[^.]*\b(loop|recursion)\b", re.I),
    )),
    ("rate limiting (not a vulnerability)", (
        re.compile(r"\b(missing|lack of|no|without)\s+rate[\s-]+limit", re.I),
        re.compile(r"\brate[\s-]+limiting\s+(is\s+)?(missing|required|not implemented|absent)", re.I),
        re.compile(r"\b(implement|add)\s+rate[\s-]+limit", re.I),
        re.compile(r"\bunlimited\s+(requests|calls|api)", re.I),
    )),
    ("resource management (not a vulnerability)", (
        re.compile(r"\b(resource|memory|file|handle)\s+leak", re.I),
        re.compile(r"\bunclosed\s+(resource|file|connection|socket|handle)", re.I),
        re.compile(r"\b(database|thread|socket|connection)\s+leak", re.I),
        re.compile(r"\b(not|never)\s+(closed|released|freed)\b", re.I),
    )),
    ("open redirect (low impact)", (
        re.compile(r"\b(open|unvalidated)\s+redirect", re.I),
        re.compile(r"\bredirect\s+(attack|exploit|vulnerability)", re.I),
    )),
    ("regex injection or regex denial of service (not applicable)", (
        re.compile(r"\b(regex|regular expression)\s+(injection|denial of service|flooding)", re.I),
        re.compile(r"\bredos\b", re.I),
        re.compile(r"\bcatastrophic backtracking\b", re.I),
    )),
    ("log spoofing (not a vulnerability)", (
        re.compile(r"\blog\s+(spoofing|injection|forging|forgery)", re.I),
    )),
    ("missing audit logging (not a vulnerability)", (
        re.compile(r"\b(lack of|missing|no|without)\s+audit\s+log", re.I),
    )),
    ("outdated dependency (managed separately)", (
        re.compile(r"\b(outdated|old|vulnerable|unpatched)\s+(version of\s+)?(third.party\s+)?(library|dependency|package|module)", re.I),
        re.compile(r"\b(known|published)\s+cve\b", re.I),
    )),
    ("missing hardening (not a concrete vulnerability)", (
        re.compile(r"\b(lack of|lacks?( an?)?|missing|no|without)\s+(hardening|security headers?|content security policy|csp|hsts|secure flag)", re.I),
    )),
    ("relies on controlling an environment variable or command-line flag (trusted values)", (
        re.compile(r"\battacker[^.]*\b(environment variable|env var|command.line (flag|argument))", re.I),
        re.compile(r"\b(environment variable|env var|command.line (flag|argument))[^.]*\b(attacker|malicious)", re.I),
    )),
]
MEMORY_SAFETY = (
    re.compile(r"\b(buffer|stack|heap)\s+overflow\b", re.I),
    re.compile(r"\b(oob|out.?of.?bounds?)\b", re.I),
    re.compile(r"\b(memory safety|memory corruption)\b", re.I),
    re.compile(r"\b(use.?after.?free|double.?free|null.?pointer.?dereference)\b", re.I),
    re.compile(r"\b(segmentation fault|segfault)\b", re.I),
    re.compile(r"\binteger\s+(overflow|underflow)\b", re.I),
)
SSRF = re.compile(r"\b(ssrf|server.?side\s+request\s+forgery)\b", re.I)


@dataclass
class Verdict:
    finding: dict
    reason: str | None  # None: kept

    @property
    def kept(self) -> bool:
        return self.reason is None


def _text(finding: dict) -> str:
    return " ".join(str(finding.get(k) or "") for k in ("title", "category", "description", "exploit_scenario"))


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def exclusion_reason(finding: dict) -> str | None:
    """Why the finding is dropped by rule, or None to keep it for the verifier."""
    path = str(finding.get("file") or "").replace("\\", "/")
    suffix = _suffix(path)
    if suffix in DOC_SUFFIXES:
        return "documentation file"
    if TEST_PATH.search(path):
        return "test-only file"
    confidence = finding.get("confidence")
    if isinstance(confidence, (int, float)) and 0 <= confidence < MIN_CONFIDENCE:
        return f"confidence {confidence:g} is below {MIN_CONFIDENCE:g} (too speculative)"
    text = _text(finding)
    for reason, patterns in RULES:
        if any(p.search(text) for p in patterns):
            return reason
    if suffix not in C_LIKE and any(p.search(text) for p in MEMORY_SAFETY):
        return "memory safety finding outside C or C++ (not applicable)"
    if suffix == ".html" and SSRF.search(text):
        return "server-side request forgery in client-side HTML (not applicable)"
    return None


def filter_findings(findings: list[dict]) -> list[Verdict]:
    return [Verdict(f, exclusion_reason(f)) for f in findings]


def parse(text: str) -> list[dict]:
    """The candidates as an agent passes them: a JSON list, or an object with a "findings" list
    (a Markdown code fence around it is tolerated)."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    data = json.loads(body or "[]")
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list) or not all(isinstance(f, dict) for f in data):
        raise ValueError("expected a JSON list of findings (objects with file, line, description, ...)")
    return data


def describe(verdicts: list[Verdict]) -> str:
    kept = [v for v in verdicts if v.kept]
    dropped = [v for v in verdicts if not v.kept]
    lines = [f"Kept {len(kept)} of {len(verdicts)} candidate(s) for verification; dropped {len(dropped)} by rule."]
    for v in dropped:
        where = f"{v.finding.get('file', '?')}:{v.finding.get('line', '?')}"
        lines.append(f"- DROPPED {where}: {v.reason}")
    lines.append("Kept candidates (JSON):")
    lines.append(json.dumps([v.finding for v in kept], indent=1))
    return "\n".join(lines)
