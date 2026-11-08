"""Key-shaped strings: replaced before text leaves the machine.

A review job sends a listing of the user's code to the model; the patterns here are replaced in
it first. They match shapes, not known secrets: a match means "this looks like a credential",
which is enough to keep it out of a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED = "[redacted]"

# (what it looks like, pattern). Where a pattern has named groups, the first group that matched
# is what gets redacted (the key's name stays, so a reviewer still sees that a password is set);
# otherwise the whole match is.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A PEM private key: the header alone counts; the block through its footer when the body is
    # base64 (with whatever `+` or `path:12| ` prefixes a diff or a listing puts in front of each line).
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\w+/=\s:,.|]*-----END [A-Z ]*PRIVATE KEY-----)?")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # OpenAI, GitHub, Slack and BharatCode tokens: a known prefix, then 10 or more word characters
    # (hyphens too: Slack tokens have them in the middle).
    ("API token", re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|xox[baprs]-|bc_live_)[\w-]{10,}")),
    ("JWT", re.compile(r"\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}")),
    # password=..., token: "...", API_KEY='...': the value, 8 or more characters, quoted or not,
    # after = or :. An unquoted value must have a digit in it, or `auth(password=password)` and
    # `token=access_token` would count; so must a quoted one, so that a label like "Password:" doesn't.
    ("password or token value", re.compile(
        r"(?i)(?:password|passwd|secret|token|api_key)[\"']?\s*[=:]\s*"
        r"(?:\"(?P<dq>(?=[^\"\s]*\d)[^\"\s]{8,})\"|'(?P<sq>(?=[^'\s]*\d)[^'\s]{8,})'"
        r"|(?P<bare>(?=[A-Za-z0-9_\-/+]*\d)[A-Za-z0-9_\-/+]{8,}))")),
)


@dataclass(frozen=True)
class Match:
    label: str  # what it looks like, e.g. "AWS access key"
    start: int
    end: int


def find(text: str) -> list[Match]:
    """Every key-shaped span in `text`, in order, none overlapping (a token inside a token=... value
    counts once)."""
    found = []
    for label, pattern in PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.span()
            for name, value in m.groupdict().items():
                if value is not None:
                    start, end = m.span(name)
                    break
            found.append(Match(label, start, end))
    found.sort(key=lambda f: (f.start, -f.end))
    kept: list[Match] = []
    for match in found:
        if kept and match.start < kept[-1].end:
            continue
        kept.append(match)
    return kept


def redact(text: str) -> tuple[str, int]:
    """`text` with every key-shaped string replaced by "[redacted]", and how many were."""
    matches = find(text)
    if not matches:
        return text, 0
    out, pos = [], 0
    for match in matches:
        out.append(text[pos:match.start])
        out.append(REDACTED)
        pos = match.end
    out.append(text[pos:])
    return "".join(out), len(matches)
