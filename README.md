# spare-gpu

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

Queues repo chores (tests, docs, code review) and sends them to the BharatCode model API only when it has spare capacity, instead of on a fixed schedule or all at once.

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

## How spare-gpu is meant to work differently

Instead of trusting a clock, spare-gpu is built to poll BharatCode's live capacity and only send work into a genuinely free slot:

- Checks real capacity before sending, rather than assuming a time window is quiet.
- One request in flight at a time, never in parallel.
- Backs off the moment the queue fills or BharatCode answers busy.
- Results land on a separate git branch for review — it never touches `main`.

That's the design the finding above points to. What's actually built so far is narrower — see the next section.

## What works today vs. what's coming

**Today:**
- A SQLite job store (`gtu add`, `gtu list`, `gtu status`, `gtu cancel`) — jobs can be queued, listed and cancelled.
- A BharatCode API client with exponential backoff on busy responses, truncation detection (a cut-off reply is kept but never auto-retried with a half-written tool call), and a full request log (tokens, latency, `x-request-id`).
- 34 tests, all against an in-process fake of the BharatCode API — no test ever calls the real one.

**Coming:**
- The capacity gate — the part that actually reads live stats and decides "send now" vs "wait."
- Job runners for `tests`, `docs` and `review` — right now a queued job is stored but nothing executes it.
- A morning report summarizing what ran overnight.

In short: the finding and the plumbing are real and tested; the scheduler that acts on the finding is not built yet.

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
- The live stats endpoints (`/api/serving/usage`, `/api/serving/history`) are undocumented, unversioned, and could change or disappear without notice.
- Two assumptions in the client are unconfirmed: that HTTP 429 should be treated like 503 (BharatCode's docs only mention 503), and that `Retry-After` is honoured when sent — it was never observed in testing.
- Only `/chat/completions` is used; `/responses` is not supported.
- This is a development-chores tool, not a general-purpose bulk prompt runner.
- The command is `gtu` and the package `gpu_token_utilization`, left over from an earlier project name.

## Research

- [research/livestats-endpoints.md](research/livestats-endpoints.md) — the live stats endpoints: what they return, how fresh, how trustworthy, and the measurement behind the finding above.
- [research/bharatcode-cli-scheduling-check.md](research/bharatcode-cli-scheduling-check.md) — whether BharatCode's own CLI and Desktop app already have a scheduling feature (checked so spare-gpu doesn't duplicate one that exists). Short answer: no.

## Licence and attribution

MIT — see [LICENSE](LICENSE). This is an independent project, built for the BharatCode Build League, and is not affiliated with or endorsed by BharatCode.
