# spare-gpu

> Your repo's night watchman. Queue the work you keep putting off; review it with your coffee.

Queue repo work (tests, docs, code review) and run it overnight through the BharatCode model API. Results wait on a separate git branch for you to review. It sends one request at a time and pauses when BharatCode is busy, so it only uses spare capacity.

**Status: Phase 1 (plumbing).** You can check your key and queue jobs. Nothing runs jobs yet; that arrives in Phase 2.

## Install (development)

```bash
python -m pip install -e ".[dev]"
```

Requires Python 3.11+. Works on Windows, macOS and Linux.

## Use

```bash
export BHARATCODE_API_KEY=...    # PowerShell: $env:BHARATCODE_API_KEY = "..."
gtu check
```

`gtu check` calls `GET /models` and prints the available models and the response time. Because `/models` answers even with a wrong key, it then sends a 1-token `POST /chat/completions` request to confirm the key is accepted. That uses about 11 tokens and takes a few seconds. It exits non-zero if the key is missing, rejected, or BharatCode is busy. The key is read only from the `BHARATCODE_API_KEY` environment variable; it is never written to a file, logged or printed.

### Queue jobs

Run these from the root of the repo you want worked on:

```bash
gtu add tests src/calc.py --note "cover negative numbers"   # kinds: tests, docs, review
gtu list            # all jobs (--active hides finished ones)
gtu status <job-id> # job details, steps, requests and tokens used
gtu cancel <job-id>
```

Jobs live in a small SQLite database at `.gtu/gtu.db` inside that repo (set `GTU_DB` to put it elsewhere). The folder gitignores itself, so it is never committed.

### How it talks to BharatCode

- One request at a time, never in parallel.
- When BharatCode is busy (HTTP 503, and 429 by our choice), it waits 5, 10, 20, then 40 seconds (plus a little randomness, max 120 s, longer if the server asks) and then gives up.
- If an answer is cut off (`finish_reason: length`), the partial text is kept, any half-written tool call is thrown away rather than run, and it is not retried automatically.
- Every attempt is logged with its time, tokens (including reasoning tokens), response time, `x-request-id`, and whether it was cut off.

## Limitations

- Phase 1: jobs can be queued but nothing runs them yet. No scheduler or capacity gate.
- Only `/chat/completions` is used; `/responses` is not supported.
- Treating HTTP 429 like 503 is our own choice; BharatCode's docs only mention 503.
- Development jobs only. This is not a general-purpose bulk prompt runner.
- The command is called `gtu` and the Python module `gpu_token_utilization`, left over from the earlier project name.

## Tests

```bash
python -m pytest
```

Tests never call the real API.

## Research

See [research/bharatcode-cli-scheduling-check.md](research/bharatcode-cli-scheduling-check.md) for the check of whether BharatCode CLI and Desktop already have scheduling features.
