"""The rule-based false-positive filter of /security-review: each known low-signal pattern is
dropped with its reason, real-looking candidates are kept, and the agent's JSON is read in the
shapes it comes in."""

import json

import pytest

from gpu_token_utilization import security

SQLI = {"file": "app/views.py", "line": 42, "severity": "high", "category": "sql_injection",
        "description": "The search parameter is formatted into the SQL string.",
        "exploit_scenario": "GET /search?q=' OR 1=1-- returns every row.", "recommendation": "Use a parameter.",
        "confidence": 0.92}


@pytest.mark.parametrize("finding, reason", [
    ({**SQLI, "file": "docs/setup.md"}, "documentation file"),
    ({**SQLI, "file": "tests/test_views.py"}, "test-only file"),
    ({**SQLI, "file": "src/views_test.go"}, "test-only file"),
    ({**SQLI, "file": "web/__tests__/views.spec.ts"}, "test-only file"),
    ({**SQLI, "confidence": 0.55}, "confidence 0.55 is below 0.7 (too speculative)"),
    ({**SQLI, "description": "An unbounded loop could exhaust CPU resources."}, "denial of service or resource exhaustion (low signal)"),
    ({**SQLI, "description": "Potential denial of service via large payloads."}, "denial of service or resource exhaustion (low signal)"),
    ({**SQLI, "description": "The endpoint has no rate limiting."}, "rate limiting (not a vulnerability)"),
    ({**SQLI, "description": "The file handle is never closed, a file leak."}, "resource management (not a vulnerability)"),
    ({**SQLI, "description": "An open redirect lets a link point elsewhere."}, "open redirect (low impact)"),
    ({**SQLI, "description": "User text goes into a regular expression: regex injection."}, "regex injection or regex denial of service (not applicable)"),
    ({**SQLI, "description": "Catastrophic backtracking on crafted input."}, "regex injection or regex denial of service (not applicable)"),
    ({**SQLI, "description": "User input reaches the log line: log injection."}, "log spoofing (not a vulnerability)"),
    ({**SQLI, "description": "There is no audit log of deletions."}, "missing audit logging (not a vulnerability)"),
    ({**SQLI, "description": "The project pins an outdated version of the yaml library with a known CVE."}, "outdated dependency (managed separately)"),
    ({**SQLI, "description": "Responses lack a Content Security Policy header."}, "missing hardening (not a concrete vulnerability)"),
    ({**SQLI, "exploit_scenario": "An attacker who sets the DATABASE_URL environment variable reaches another host."},
     "relies on controlling an environment variable or command-line flag (trusted values)"),
    ({**SQLI, "file": "src/parse.py", "description": "A buffer overflow in the parser."}, "memory safety finding outside C or C++ (not applicable)"),
    ({**SQLI, "file": "web/index.html", "description": "SSRF through the image URL."}, "server-side request forgery in client-side HTML (not applicable)"),
])
def test_known_false_positive_patterns_are_dropped_with_their_reason(finding, reason):
    assert security.exclusion_reason(finding) == reason


@pytest.mark.parametrize("finding", [
    SQLI,
    {**SQLI, "file": "src/parse.c", "description": "A buffer overflow in the parser lets a crafted packet overwrite the return address."},
    {**SQLI, "file": "api/fetch.py", "category": "ssrf", "description": "SSRF: the host of the fetched URL comes from the request."},
    {**SQLI, "confidence": 0.7},
    {**SQLI, "confidence": None},
    {"file": "app/auth.py", "line": 10, "description": "The password check compares against a hard-coded value.", "exploit_scenario": "Anyone sending that value signs in as admin."},
])
def test_real_looking_candidates_are_kept(finding):
    assert security.exclusion_reason(finding) is None


def test_filter_describes_what_was_dropped_and_returns_the_kept_ones_as_json():
    verdicts = security.filter_findings([SQLI, {**SQLI, "file": "README.md", "line": 3}])
    assert [v.kept for v in verdicts] == [True, False]
    text = security.describe(verdicts)
    assert text.startswith("Kept 1 of 2 candidate(s) for verification; dropped 1 by rule.")
    assert "- DROPPED README.md:3: documentation file" in text
    kept = json.loads(text.split("Kept candidates (JSON):", 1)[1])
    assert kept == [SQLI]


def test_the_agents_json_is_read_in_its_shapes():
    assert security.parse(json.dumps([SQLI])) == [SQLI]
    assert security.parse(json.dumps({"findings": [SQLI]})) == [SQLI]
    assert security.parse("```json\n" + json.dumps([SQLI]) + "\n```") == [SQLI]
    assert security.parse("  ") == []
    with pytest.raises(ValueError, match="expected a JSON list"):
        security.parse(json.dumps({"findings": "nope"}))
    with pytest.raises(ValueError):
        security.parse("not json")
