# spare-gpu

Queue the repo chores you keep putting off — tests, docs, code review — and let them run on BharatCode's spare capacity, each on its own git branch.

## Quickstart

spare-gpu runs on Windows (PowerShell).

```powershell
pipx install spare-gpu                     # or: pip install spare-gpu
$env:BHARATCODE_API_KEY = "bc_live_..."
cd your-repo
gtu demo                                # a short code review of one of your files, in a minute or two
```

Then queue real work, which runs when BharatCode has capacity:

```powershell
gtu add review src/app.py    # or: tests, docs
gtu start                    # gtu why — what it's waiting for;  gtu report — what got done
```

Get a key at [bharatcode.ai/account](https://bharatcode.ai/account). You need Python 3.11+ and pipx; see [Install and try it](#install-and-try-it).

---

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

**Status: v0.1.0.** Tests, docs and review jobs do real work on their own git branch, one step at a time while BharatCode has spare capacity, within token budgets, and survive being stopped. Tested end to end against fakes of BharatCode's API and CLI; not yet run end to end against the real service.

## The finding

The premise behind "just run your AI chores overnight" is that BharatCode is quiet at night. We measured it instead of assuming it.

On 2026-10-29, we polled BharatCode's public live-stats endpoint (`/api/serving/usage`, undocumented, no auth needed — see [research/livestats-endpoints.md](research/livestats-endpoints.md)) once a minute. At **01:30 IST**, the Qwen 3.8 27B API lane was running all 4 of its slots and had **13 requests queued** — 100% capacity used. The endpoint's per-lane figures show this directly; totals across both models hide which lane is full.

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

### Since then: pacing, and an open question

The next day BharatCode announced **account-level pacing**: requests from an account can be held back even when slots are free, to share capacity fairly. Two things we've measured since change the picture:

- A queue is often present while slots are idle (it's pacing, not load), so "queued requests" alone doesn't mean "busy".
- BharatCode reports how long it held each request (`x-bharatcode-queue-wait-ms`). In our first real tests, requests sent straight into a **full** lane waited only **3–15 seconds**, while waiting for the gate to see a free slot once took **11 minutes**.

So whether waiting for capacity actually beats just sending is now an open question. A 24-hour experiment is measuring it (see [Research](#research)); the gate below is the current design, not a settled answer.

## How spare-gpu works

Instead of trusting a clock, spare-gpu reads BharatCode's live capacity before sending:

- Checks real capacity before sending, rather than assuming a time window is quiet.
- One request in flight at a time, never in parallel.
- Holds off while every slot in the lane is busy, the queue is longer than a tolerance, or BharatCode has just answered busy.
- Results land on a separate git branch for review — it never touches `main`.

## What works today vs. what's coming

**Today:**
- A SQLite job store (`gtu add`, `gtu list`, `gtu status`, `gtu cancel`) — jobs can be queued, listed and cancelled.
- A capacity gate (`gtu why`, `gtu start`) that polls BharatCode's live stats, falls back to our own request history when that's unusable, and only sends work while it's genuinely quiet.
- A runner (`gtu start`) that works through the queue one step at a time, survives being stopped (Ctrl+C, a crash, closing the terminal) and resumes without redoing finished steps.
- Three job types — `tests`, `docs`, `review` — each working in its own git worktree on branch `gtu/<job-id>` (see below).
- Per-job and per-night token budgets that pause work when reached.
- A BharatCode API client with exponential backoff on busy responses, truncation detection (a cut-off reply is kept but never auto-retried with a half-written tool call), and a full request log (tokens, latency, `x-request-id`, BharatCode's reported queue wait, and what the gate saw when each request was sent).
- `gtu demo` (see it work in a minute or two), `gtu start --now` (skip the wait), `gtu start --dry-run` (what would run and roughly what it would cost), `gtu stop` (halt cleanly; jobs pause), and `gtu report` (the morning summary).
- Progress output while it waits: why the gate is closed, when it checks next, and every retry wait.
- Tests against in-process fakes of the BharatCode API and CLI — no test ever calls the real service.

**Coming:**
- Real end-to-end runs against BharatCode, and the gate-vs-send experiment's results, which may change how the gate works.

## Install and try it

| Route | Command | Needs |
|---|---|---|
| pipx (recommended) | `pipx install spare-gpu` | Python 3.11+ |
| pip | `pip install spare-gpu` | Python 3.11+ |
| From source | `python -m pip install -e ".[dev]"` | Python 3.11+ |

Windows only (10 and 11); macOS and Linux aren't supported. Every route needs `git`; `tests` and `docs` jobs also need BharatCode's own CLI (`npm install -g bharatcode`, then `bharatcode auth login`).

**Installing with pipx on Windows**, from PowerShell:

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/) (tick "Add python.exe to PATH"), or `winget install Python.Python.3.12`.
2. `python -m pip install --user pipx`, then `python -m pipx ensurepath`.
3. Close and reopen PowerShell so the new PATH takes effect.
4. `pipx install spare-gpu`, then `gtu --version`.

If `gtu` is "not recognized" after step 4, PATH wasn't refreshed: open a new terminal, or run `python -m pipx ensurepath` again.

There is no standalone `gtu.exe`: an unsigned executable is blocked by Windows Smart App Control, so pipx is the supported route. Step-by-step checks for a clean machine are in [docs/install-checklist.md](docs/install-checklist.md).

```powershell
gtu demo [file]   # one short review, sent straight away, to see it work
```

```powershell
$env:BHARATCODE_API_KEY = "..."
gtu check
```

`gtu check` calls `GET /models`, then sends a 1-token `POST /chat/completions` to confirm the key is actually accepted (`/models` answers even with a wrong key). Exits non-zero if the key is missing, rejected, or BharatCode is busy.

```powershell
gtu add tests src/calc.py --note "cover negative numbers"   # kinds: tests, docs, review
gtu list            # all jobs (--active hides finished ones)
gtu status <job-id> # job details, steps, requests and tokens used
gtu cancel <job-id>
```

Jobs live in `.gtu/gtu.db` inside the repo you run them from (`GTU_DB` to move it). That folder gitignores itself, so it's never committed.

### Run jobs

```powershell
gtu why     # would it send work right now, and why
gtu start   # work through the queue, one step at a time, until it is empty
gtu start --now   # same, but don't wait for spare capacity
gtu start --dry-run   # which jobs and steps would run, and roughly how many tokens; sends nothing
gtu stop          # from another terminal: stop it now; jobs are paused, not failed
gtu report        # morning summary; also committed to each finished job's branch
```

While it waits, `gtu start` says why, for example `Gate closed: qwen-3.8-27b api lane 4/4 slots busy, 3 queued. Next check in 60s (opens after 3 quiet readings in a row; 0 so far).` and `BharatCode is busy (HTTP 503). Waiting 160s before retry 6 of 8.`

`gtu report` covers everything since tonight began (12:00 IST; `--since <hours>`, `--job <id>` or `--all` to change that): what ran and when, how long it took, tokens per job and in total, the files each job added or changed, tests kept and discarded, and the capacity it met — queue depth and BharatCode's reported queue wait at send time, and the share of requests sent into a free slot.

`gtu stop` asks the running `gtu start` to stop (it notices within about a second, even mid-step), and kills it if it hasn't stopped after 10 seconds. Either way, any running BharatCode CLI process is killed, the unfinished step's edits are discarded, finished steps stay committed on the job's branch, and the job is marked paused. `gtu start` picks up where it left off.

`--now` sends work straight away, competing with everyone else's requests instead of using spare capacity. It still records the capacity each request was sent into, still retries busy answers politely (about 15 minutes, then a one-minute pause), and still respects token budgets.

`gtu start` only sends a request while the **capacity gate** is open. Stop it any time (Ctrl+C, closing the terminal, a crash); run it again and it continues from the last finished step without redoing anything.

### The three job types

Every job works in its own git worktree (`.gtu/worktrees/<job-id>`) on branch **`gtu/<job-id>`**, made from your current commit. Your checkout is only ever read; results are committed to the job's branch for you to review and merge yourself. `gtu add` refuses a repo with uncommitted changes or untracked files, so the branch starts from exactly what you see.

| Job | Who does the work | Steps | What lands on the branch |
|---|---|---|---|
| `tests` | BharatCode's CLI (`bharatcode run`) | **write**: the CLI writes new test files and may run the allowlisted test command. **verify**: we run each new test file ourselves, delete failing tests, discard files that still fail or don't run, and revert any change that isn't a new test file. | Only tests that pass. |
| `docs` | BharatCode's CLI | **write**: the CLI adds docstrings and README text (no shell at all). **verify**: a Python file is kept only if its code is unchanged apart from docstrings and no existing docstring was removed or shortened; a Markdown file only if no line was removed. Everything else is reverted. | Only additive documentation changes. |
| `review` | Our own client | **review**: sends the code with line numbers and writes the answer to `gtu-review-<job-id>.md`. | One report file; no source changes. |

The CLI's output is saved as a patch outside the worktree before it's checked, so unchecked work is never committed and a crash during checking never loses it.

**Safeguards on the CLI.** It runs only in the job's worktree, with `--pure` (your global BharatCode plugins are off), one call per step under a timeout (`GTU_CLI_TIMEOUT`, default 900 s), and a generated `opencode.json` that denies every shell command except the allowlist (`GTU_ALLOWED_COMMANDS`, default `python -m pytest, pytest, npm test, go test`) and denies web access. That file is never committed. The CLI is started directly, not through a shell, so nothing in a prompt is interpreted by one. If your main checkout changes while a step runs, the job stops.

**What we can't see.** The CLI's requests don't go through our client, so for tests and docs jobs we log only what the CLI reports: tokens per step and timings, not `x-request-id` or queue wait. Each CLI step costs about 11,500 tokens, mostly the CLI's own system prompt; a tests or docs job typically uses 50–150k. A one-run check suggested the CLI's traffic uses the same API lane the gate watches.

### Token budgets

Reaching a budget pauses the job; it never fails it. A CLI run is killed as soon as its tokens cross what's left, and the half-done step is discarded and rerun later.

| Variable | Default | Meaning |
|---|---|---|
| `GTU_JOB_TOKEN_BUDGET` | `200000` | Most tokens one job may use. |
| `GTU_NIGHT_TOKEN_BUDGET` | `1000000` | Most tokens all jobs together may use per night. |
| `GTU_NIGHT_START` | `12:00` | When a "night" starts (IST); it lasts 24 hours, so one overnight session counts once. |
| `GTU_TEST_COMMAND` | `python -m pytest` | How the tests job runs tests. Must be in the allowlist. |
| `GTU_TEST_TIMEOUT` | `300` | Seconds per test run. |

### The capacity gate

- **Main signal: BharatCode's public live stats** (`/api/serving/usage`), checked at most once a minute. It counts as quiet when the model is up and a slot in the API lane is free, as long as the queue is within a tolerance (see below).
- **It changes its mind slowly.** It needs 3 quiet readings in a row (about 3 minutes) to open and 2 busy ones to close. It closes at once if BharatCode answers one of our requests with "busy".
- **Fallback.** That endpoint is undocumented, so any odd answer (error, timeout, changed shape, stale data, model missing) switches to our own signals: how our last 5 requests went (busy answers, slowdowns), plus a tiny probe request at most every 5 minutes when there's been no traffic. In fallback it also only runs inside a time window, `GTU_WINDOW` (default `00:00-09:00` IST).
- The window isn't used while live stats work, because the finding above showed nights in India are not reliably quiet. Set `GTU_WINDOW_ALWAYS=1` to enforce it anyway.
- **The queue is a tolerance, not a blocker.** BharatCode paces accounts to free capacity for low-usage users, so requests can sit queued while slots are idle. "Quiet" therefore means the model is up and a slot is free; the queue only counts once it is longer than `GTU_MAX_QUEUED` (default `5`). Use `GTU_MAX_QUEUED=0` for the original strict rule (any queue means busy), or `none` to ignore the queue.
- Every reading is stored, and every request records what the gate saw when it was sent (source, queue length, busy slots).

### How it talks to BharatCode

- One request at a time, never in parallel.
- **Patient by default.** BharatCode paces accounts and has asked automated clients to raise their timeout and retry limits, so:
  - each request may take up to **180 s** before it counts as timed out;
  - when BharatCode is busy (HTTP 503, and 429 by our choice) or a request times out, the client retries **8 times**, waiting about 5, 10, 20, 40, 80, 160, 300 and 300 seconds (each ±20% so clients don't retry in lockstep, never more than 300 s, longer if the server sends `Retry-After`). That's about 15 minutes before it gives up.
- `gtu start` turns these client retries off and lets the gate decide when to try again: a busy answer closes the gate, and it reopens after 3 quiet readings. So `gtu start` itself never gives up; it just waits.

All of it is configurable:

| Variable | Default | Meaning |
|---|---|---|
| `GTU_TIMEOUT` | `180` | Seconds to wait for a model response (also used by `gtu check`). |
| `GTU_MAX_RETRIES` | `8` | Retries after a busy answer or timeout, before giving up. |
| `GTU_RETRY_BASE` | `5` | First wait in seconds; each later wait doubles. |
| `GTU_RETRY_MAX_DELAY` | `300` | Longest single wait in seconds. |
| `GTU_RETRY_JITTER` | `0.2` | Random spread on each wait (0.2 = ±20%). |

- If an answer is cut off (`finish_reason: length`), the partial text is kept, any half-written tool call is thrown away rather than run, and it is not retried automatically.
- Every attempt is logged with when it was sent, tokens (including reasoning tokens), response time, `x-request-id`, BharatCode's reported queue wait (`x-bharatcode-queue-wait-ms`), and whether it was cut off.

## Running the tests

```powershell
python -m pytest
```

No test calls the real BharatCode API or CLI; they use in-process fakes of both, plus real git and real pytest runs in temporary folders.

## Releases

Releases are published to PyPI by hand; there is no release workflow. To cut one: bump `__version__` in `src/gpu_token_utilization/__init__.py`, run the tests and the [install checklist](docs/install-checklist.md), then:

```powershell
python -m pip install -e ".[build]"
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

## What it will never do

- Never touches your `main` branch or your checkout; all work happens in a worktree on `gtu/<job-id>`.
- Never pushes to GitHub on its own.
- Only runs allowlisted test commands, under a timeout. Note that this still means **running tests the model wrote** on your machine, inside the job's worktree; read what a tests job will run if that matters to you.
- Your API key stays in the `BHARATCODE_API_KEY` environment variable — it is never written to a file, logged, or sent anywhere except to BharatCode.

## Honest limitations

- The finding above is a single-user, single-night measurement, not a study of BharatCode's aggregate load. It shows the premise can be false; it doesn't show how often.
- The live stats endpoints (`/api/serving/usage`, `/api/serving/history`) are undocumented, unversioned, and could change or disappear without notice. The gate is built to fall back to our own signals when that happens.
- Measured queue waits vary a lot. BharatCode's reported wait (`x-bharatcode-queue-wait-ms`) was 0.3–12 s overnight on 30 Oct (03:10–05:10 IST), but 31–72 s between 08:10 and 08:50 IST. That gap isn't only time of day: at 05:30 IST (00:00 UTC) BharatCode cut the Qwen API lane from 4 slots to 2, so the overnight and morning numbers come from different capacity. These are early numbers: the full 24-hour gate-vs-send experiment is still running, and its results may change how the gate works.
- We assume the API lane's queue is the one our requests join. A free slot does not guarantee a prompt start: under pacing, BharatCode can hold a request for several seconds either way, and once answered busy (503) after holding it for 5.6 s.
- Two assumptions in the client are unconfirmed: that HTTP 429 should be treated like 503 (BharatCode's docs only mention 503), and that `Retry-After` is honoured when sent — it was never observed in testing.
- `gtu start` stops when the queue is empty; it does not wait for new jobs.
- The job types are tested against fakes of BharatCode's API and CLI, not yet end to end against the real service.
- Failing generated tests are dropped per test for Python (pytest); for other languages a whole test file is kept or discarded.
- A tests job only accepts new test files; it never edits your existing tests.
- A CLI step that times out is retried after the gate reopens; a task that always times out will keep retrying until its budget runs out.
- Only `/chat/completions` is used; `/responses` is not supported.
- This is a development-chores tool, not a general-purpose bulk prompt runner.
- The command is `gtu` and the package `gpu_token_utilization`, left over from an earlier project name.

## Research

- [research/livestats-endpoints.md](research/livestats-endpoints.md) — the live stats endpoints: what they return, how fresh, how trustworthy, the measurement behind the finding above, and the capacity gate design it led to.
- [research/bharatcode-cli-scheduling-check.md](research/bharatcode-cli-scheduling-check.md) — whether BharatCode's own CLI and Desktop app already have a scheduling feature (checked so spare-gpu doesn't duplicate one that exists). Short answer: no.
- [scripts/livestats_log.py](scripts/livestats_log.py) — logs the live stats once a minute (Qwen and DeepSeek API lanes, plus totals) to CSV, to build a dataset of when BharatCode actually has spare capacity.
- [scripts/gate_vs_send.py](scripts/gate_vs_send.py) — the gate-vs-send experiment: every 20 minutes it records what the gate would decide, then sends one tiny request anyway and records BharatCode's reported queue wait. Results will be in `research/gate-vs-send-experiment.md`.

## Licence and attribution

MIT — see [LICENSE](LICENSE). This is an independent project, built for the BharatCode Build League, and is not affiliated with or endorsed by BharatCode.
