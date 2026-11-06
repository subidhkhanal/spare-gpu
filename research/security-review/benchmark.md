# /security-review benchmark (plan §8)

**Status: designed, not run.** Needs BharatCode Desktop on Windows. No number below is a result.

## Ship rule

Precision at or above the built-in `/review` on seeded vulnerabilities (findings that are real,
judged by a person), and no exploit code executed in any run (checked in the session logs).

## Material

- 20 seeded vulnerabilities across 4 small web services (Python/Flask, Node/Express, Go, and
  one with a database layer): SQL injection, command injection, path traversal, a hard-coded
  secret, a JWT check that accepts `alg: none`, an eval on request data, a pickle load of an
  upload, a cookie without the HttpOnly flag (a hardening item that must NOT be reported),
  reflected XSS in a server-rendered template, a missing authorisation check on a delete
  endpoint, and so on. Each is one uncommitted change with its file:line recorded.
- 10 clean or false-positive-bait diffs: a removed rate limit, a long regex, a change in a
  `tests/` folder, a README edit, an outdated dependency bump.
- The MIT repo's evaluation idea (claude-code-security-review `evals/`): one run per diff,
  findings compared to the seed list.

## Method

Run `/review` (built in) and `/security-review` on every diff. A finding matches a seed when it
names the seeded line (or the line that fails because of it) and the right category. Record per
run: findings, dropped-by-rule count, verifier scores, minutes, model per phase, tokens per model.

## Results

| Setup | Recall (of 20) | Precision | Dropped by rule | Minutes (median) |
|---|---|---|---|---|
| built-in /review | | | | |
| /security-review | | | | |
| /security-review deep | | | | |
