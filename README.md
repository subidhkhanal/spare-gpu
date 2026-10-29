# spare-gpu

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

Queues repo chores (tests, docs, code review) and sends them to the BharatCode model API only when it has spare capacity, instead of on a fixed schedule or all at once.

**Status: Phase 2 (capacity gate and runner).** Jobs run one step at a time while BharatCode has spare capacity, and survive being stopped. The only work each job does so far is a short "survey" (a plan of what it would do); the real tests/docs/review work arrives in Phase 3.

## The finding

The premise behind "just run your AI chores overnight" is that BharatCode is quiet at night. We measured it instead of assuming it.

On 2026-10-29, we polled BharatCode's public live-stats endpoint (`/api/serving/usage`, undocumented, no auth needed — see [research/livestats-endpoints.md](research/livestats-endpoints.md)) once a minute. At **01:30 IST**, the Qwen 3.8 27B API lane was running all 4 of its slots and had **13 requests queued** — 100% capacity used. At the same moment, BharatCode's public combined Live Stats chart looked unremarkable, because it blends Qwen with DeepSeek (which was near-idle) and shows totals, not per-model queue depth.

| IST time | Qwen API: active/queued (slots, used) |
|---|---|
| 01:29 | 4 / 13 (4, 100%) |
| 01:30 | 4 / 13 (4, 100%) |
| 01:31 | 4 / 12 (4, 100%) |
| 01:32 | 4 / 13 (4, 100%) |
| 01:33 | 4 / 12 (4, 100%) |
| 01:34 | 4 / 9 (4, 100%) |
| 01:35 | 4 / 6 (4, 100%) |
| 01:36 | 4 / 6 (4, 100%) |
| 01:37 | 4 / 6 (4, 100%) |
| 01:38 | 4 / 5 (4, 100%) |

Full data and method: [research/livestats-endpoints.md](research/livestats-endpoints.md).

Two things follow from this:

1. **"Run it overnight" is not a reliable strategy.** The lane most relevant to this project was saturated through the middle of the Indian night.
2. **A fixed-time scheduler (e.g. "always run 00:00–09:00 IST") fires blindly into whatever load actually exists.** It has no way to know the lane is full, so it would queue work behind 13 other requests anyway.

## How spare-gpu works differently

Instead of trusting a clock, spare-gpu polls BharatCode's live capacity and only sends work into a genuinely free slot:

- Checks real capacity before sending, rather than assuming a time window is quiet.
- One request in flight at a time, never in parallel.
- Backs off the moment the queue fills or BharatCode answers busy.
- Results land on a separate git branch for review — it never touches `main`.

## What works today vs. what's coming

**Today:**
- A SQLite job store (`gtu add`, `gtu list`, `gtu status`, `gtu cancel`) — jobs can be queued, listed and cancelled.
- A capacity gate (`gtu why`, `gtu start`) that polls BharatCode's live stats, falls back to our own request history when that's unusable, and only sends work while it's genuinely quiet.
- A runner (`gtu start`) that works through the queue one step at a time, survives being stopped (Ctrl+C, a crash, closing the terminal) and resumes without redoing finished steps. So far each job's only step is a short "survey" — a plan of what the real work would be, not the work itself.
- A BharatCode API client with exponential backoff on busy responses, truncation detection (a cut-off reply is kept but never auto-retried with a half-written tool call), and a full request log (tokens, latency, `x-request-id`, and what the gate saw when each request was sent).
- Tests against an in-process fake of the BharatCode API — no test ever calls the real one.

**Coming:**
- Job runners that do the actual tests/docs/review work, not just a survey of it (Phase 3).
- A morning report summarizing what ran overnight.
- Putting results on a separate git branch (designed for, not wired up yet).

## Install and try it

```bash
python -m pip install -e ".[dev]"
```

Requires Python 3.11+. Works on Windows, macOS and Linux.

```bash
export BHARATCODE_API_KEY=...    # PowerShell: $env:BHARATCODE_API_KEY = "..."
gtu check
```

`gtu check` calls `GET /models`, then sends a 1-token `POST /chat/completions` to confirm the key is actually accepted (`/models` answers even with a wrong key). Exits non-zero if the key is missing, rejected, or BharatCode is busy.

```bash
gtu add tests src/calc.py --note "cover negative numbers"   # kinds: tests, docs, review
gtu list            # all jobs (--active hides finished ones)
gtu status <job-id> # job details, steps, requests and tokens used
gtu cancel <job-id>
```

Jobs live in `.gtu/gtu.db` inside the repo you run them from (`GTU_DB` to move it). That folder gitignores itself, so it's never committed.

### Run jobs

```bash
gtu why     # would it send work right now, and why
gtu start   # work through the queue, one step at a time, until it is empty
```

`gtu start` only sends a request while the **capacity gate** is open. Stop it any time (Ctrl+C, closing the terminal, a crash); run it again and it continues from the last finished step without redoing anything.

### The capacity gate

- **Main signal: BharatCode's public live stats** (`/api/serving/usage`), checked at most once a minute. It counts as quiet only when the model is up, a slot in the API lane is free, **and** nothing is queued. Queued requests have been seen while slots sat idle, so neither check is enough alone.
- **It changes its mind slowly.** It needs 3 quiet readings in a row (about 3 minutes) to open and 2 busy ones to close. It closes at once if BharatCode answers one of our requests with "busy".
- **Fallback.** That endpoint is undocumented, so any odd answer (error, timeout, changed shape, stale data, model missing) switches to our own signals: how our last 5 requests went (busy answers, slowdowns), plus a tiny probe request at most every 5 minutes when there's been no traffic. In fallback it also only runs inside a time window, `GTU_WINDOW` (default `00:00-09:00` IST).
- The window isn't used while live stats work, because the finding above showed nights in India are not reliably quiet. Set `GTU_WINDOW_ALWAYS=1` to enforce it anyway, and `GTU_MAX_QUEUED=1` to tolerate a short queue.
- Every reading is stored, and every request records what the gate saw when it was sent (source, queue length, busy slots).

### How it talks to BharatCode

- One request at a time, never in parallel.
- When BharatCode is busy (HTTP 503, and 429 by our choice), the client can wait 5, 10, 20, then 40 seconds (plus a little randomness, max 120 s, longer if the server asks) before giving up. `gtu start` turns these retries off and lets the gate decide when to try again.
- If an answer is cut off (`finish_reason: length`), the partial text is kept, any half-written tool call is thrown away rather than run, and it is not retried automatically.
- Every attempt is logged with its time, tokens (including reasoning tokens), response time, `x-request-id`, and whether it was cut off.

```bash
python -m pytest
```

## What it will never do

- Never touches your `main` branch.
- Never pushes to GitHub on its own.
- Will only run allowlisted commands, under a timeout, once job runners exist.
- Your API key stays in the `BHARATCODE_API_KEY` environment variable — it is never written to a file, logged, or sent anywhere except to BharatCode.

## Honest limitations

- The finding above is a single-user, single-night measurement, not a study of BharatCode's aggregate load. It shows the premise can be false; it doesn't show how often.
- The live stats endpoints (`/api/serving/usage`, `/api/serving/history`) are undocumented, unversioned, and could change or disappear without notice. The gate is built to fall back to our own signals when that happens.
- We assume the API lane's queue is the one our requests join, and that a free slot means our request starts promptly. Both are guesses.
- Two assumptions in the client are unconfirmed: that HTTP 429 should be treated like 503 (BharatCode's docs only mention 503), and that `Retry-After` is honoured when sent — it was never observed in testing.
- `gtu start` stops when the queue is empty; it does not wait for new jobs.
- Each job only produces a short survey/plan right now; no tests, docs or reviews are actually written yet, and nothing touches git.
- Only `/chat/completions` is used; `/responses` is not supported.
- This is a development-chores tool, not a general-purpose bulk prompt runner.
- The command is `gtu` and the package `gpu_token_utilization`, left over from an earlier project name.

## Research

- [research/livestats-endpoints.md](research/livestats-endpoints.md) — the live stats endpoints: what they return, how fresh, how trustworthy, the measurement behind the finding above, and the capacity gate design it led to.
- [research/bharatcode-cli-scheduling-check.md](research/bharatcode-cli-scheduling-check.md) — whether BharatCode's own CLI and Desktop app already have a scheduling feature (checked so spare-gpu doesn't duplicate one that exists). Short answer: no.
- [scripts/livestats_log.py](scripts/livestats_log.py) — logs the Qwen API lane once a minute to a CSV, to build a dataset of when BharatCode actually has spare capacity.

## Licence and attribution

MIT — see [LICENSE](LICENSE). This is an independent project, built for the BharatCode Build League, and is not affiliated with or endorsed by BharatCode.
