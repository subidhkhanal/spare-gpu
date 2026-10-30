# spare-gpu

**A harness for BharatCode that does your repo chores unattended: tests, docs and code review, run on BharatCode's spare capacity, each on its own git branch.**

A harness is software that combines an AI model with prompts, tools and a workflow to solve a real problem. spare-gpu combines BharatCode's model with:

- **prompts** written for each chore (write passing tests, add docs without touching code, review with line references);
- **tools**: BharatCode's own CLI as the worker, plus an MCP server so you can drive it by asking in plain words;
- **capacity awareness**: it reads BharatCode's live load and only sends work when there's a free slot;
- **git isolation**: every job works in its own worktree on branch `gtu/<job-id>`; your branch and files are never touched;
- **verification**: generated tests are run and only passing ones are kept; docs changes are checked to be docs-only; anything else is reverted;

so coding work can run overnight, safely, without you watching it.

> **Windows-first.** spare-gpu is built and tested for Windows 10 and 11, and CI runs on Windows only. It may work on macOS or Linux, but it isn't supported or tested there: stopping a running job, in particular, is known to fail on both.

## Get started

All in **PowerShell** (Start menu → type "PowerShell" → Enter). If anything goes wrong, see [Troubleshooting](#troubleshooting).

**1. Install Python and Git** (skip what you already have):

```powershell
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
```

No winget? Download [Python](https://www.python.org/downloads/windows/) (tick "Add python.exe to PATH") and [Git](https://git-scm.com/download/win) by hand. Then **close PowerShell and open a new one**.

**2. Install spare-gpu:**

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

**Close PowerShell and open a new one again**, then:

```powershell
pipx install spare-gpu
```

Until spare-gpu is published on PyPI, use `pipx install git+https://github.com/subidhkhanal/spare-gpu` instead.

**3. Save your BharatCode API key** (get one at [bharatcode.ai/account](https://bharatcode.ai/account)):

```powershell
gtu setkey
```

Paste the key when asked; nothing shows while you paste, that's on purpose. It's checked and saved for your Windows account, so every terminal finds it.

**4. See it work**, in a folder with some of your code:

```powershell
gtu demo
```

A short code review of one of your files, usually within a minute or two.

**5. Queue real work** in a git repo (commit your changes first: jobs start from your last commit):

```powershell
gtu add review src/app.py      # or: gtu add tests src/payments, gtu add docs src/utils
gtu start                      # runs the queue while BharatCode has spare capacity; leave it running overnight
gtu report                     # in the morning: what ran, what it produced, what it cost
```

Each job's work lands on its own branch: look with `git diff HEAD...gtu/<job-id>` and merge what you like. `gtu why` says what it's waiting for; `gtu stop` pauses it. `tests` and `docs` jobs also need BharatCode's CLI; see [Command-line reference](#command-line-reference).

## Power-user route: ask BharatCode (MCP)

If you work inside BharatCode's CLI, you don't need the commands above day to day: spare-gpu includes an [MCP](https://modelcontextprotocol.io) server, so you ask the agent in plain words and it calls spare-gpu for you. It comes with the same install.

**Set it up once** (after step 3 above, with BharatCode's CLI installed: `npm.cmd install -g bharatcode`, then `bharatcode.cmd auth login`):

```powershell
gtu integrate
```

It finds BharatCode's config file (`%APPDATA%\BharatCode\Config\opencode.jsonc`), backs it up, adds spare-gpu's entry, keeps everything else, and checks BharatCode reports `spare-gpu connected`. It prints exactly what it changed and how to undo it (`gtu integrate --uninstall`), and running it again changes nothing. (`gtu setup` does steps 3 and this together.) The entry it adds, if you'd rather paste it yourself:

```jsonc
{
  "mcp": {
    "spare-gpu": { "type": "local", "command": ["gtu", "mcp"], "enabled": true }
  }
}
```

**Then just ask**, in BharatCode, in one of your git repos:

| You say | Tool the agent uses |
|---|---|
| "queue tests for the payments module", "get docs written for src/utils overnight" | `spare_queue_work`: queues the job and starts spare-gpu's background runner, which keeps going after you close BharatCode |
| "what's queued?", "is my tests job done?", "why hasn't my job started?" | `spare_work_status`: queued, running and finished work, and why anything is waiting |
| "what did spare-gpu do last night?" | `spare_last_night`: what ran, what each job produced, what it cost |
| "cancel the docs job" | `spare_cancel_work` |
| "is now a good time to run a big refactor?" | `spare_capacity_advice`: BharatCode's load now, its quietest hours over the past week, and how long your own requests waited |
| "how much of that was CLI overhead?" | `spare_token_cost`: tokens per job, split into overhead and task (see below) |
| "run the test suite when BharatCode is free" | `spare_run_when_free`: runs a test, build or lint command, or `bharatcode run`, once there's capacity (an allowlist enforced in code; no shell) |

**Why seven tools:** an agent picks the right tool less reliably as the number of tools grows and when tools overlap, so related questions are merged into one task-shaped tool (one status tool answers "what's queued", "is it done" and "why isn't it running") rather than one tool per internal function.

Name the folder or file when you can ("src/payments" rather than "the payments module"); the agent otherwise has to search for it first.

## Token overhead: what you pay before your prompt

Every request a coding agent sends carries its own system prompt and the descriptions of every tool it can use, before your actual request. Divide the total tokens by the tokens that were really about your task, and you get the **overhead multiplier**. It matters most on a fixed allowance, like a student's 10M tokens a day: an agent that takes 10 steps pays that fixed part 10 times.

**Our measurements** (BharatCode CLI 1.15.46, 31 Oct 2026; method and raw numbers in [research/token-overhead.md](research/token-overhead.md)):

| Before a one-line prompt, BharatCode's CLI sent | Tokens |
|---|---|
| its system prompt | about 2,000 |
| plus its built-in tool schemas (the floor, in an empty folder) | **11,504** |
| plus spare-gpu's seven MCP tools | **13,833** (+2,329) |

That's paid again on **every agent step**. spare-gpu's own tests and docs jobs switch its MCP tools off inside the CLI, so they don't pay the extra 2.3k.

**For comparison, published benchmarks** (not our measurements): the Systima Applied Research team (July 2026) measured about **7,000** tokens of system prompt, tool schemas and scaffolding for OpenCode, which BharatCode's CLI is built on, and about **33,000** for Claude Code, before reading the user's prompt. (Figures as reported in [this write-up of the Systima benchmark](https://www.kunalganglani.com/blog/opencode-vs-claude-code-token-overhead); we couldn't find Systima's original report.)

**See your own numbers:**

```powershell
gtu cost            # the last 24 hours: overhead, task, and the overhead multiplier, per job
gtu cost <job-id>   # one job
gtu cost all
```

For BharatCode CLI steps, spare-gpu counts the measured ~11,500-token floor as overhead and the rest as task. The CLI reports one input number per step, so the floor is our measurement rather than per-request data; set `GTU_CLI_OVERHEAD_TOKENS` to use your own. Reviews, which spare-gpu sends directly, have no harness overhead (about 1.0x).

---

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

**Status: v0.1.0.** Tests, docs and review jobs do real work on their own git branch, one step at a time while BharatCode has spare capacity, within token budgets, and survive being stopped. A review job has run end to end against the real service; tests and docs jobs are so far tested end to end only against fakes of BharatCode's API and CLI.

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
- An MCP server inside BharatCode's CLI with seven task-shaped tools: queue, check, report and cancel work by asking in plain words, plus capacity advice, token cost with the overhead multiplier, and run-any-allowed-command-when-free. `gtu integrate` registers it (with a backup and an undo), and queueing a job starts a background runner that outlives the BharatCode session.
- `gtu advice` (is now a good time?), `gtu cost` (overhead, task and the overhead multiplier per job) and `gtu run-when-free` (a test, build or lint command, once there's capacity).
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

## Command-line reference

### Install and try it

The [Get started](#get-started) steps above are the whole install. In short:

| Route | Command | Needs |
|---|---|---|
| pipx (recommended) | `pipx install spare-gpu` | Python 3.11+, Git |
| pip | `py -m pip install spare-gpu` | Python 3.11+, Git |
| From source | `py -m pip install -e ".[dev]"` | Python 3.11+, Git |

Windows only (10 and 11); macOS and Linux aren't supported. It works the same in PowerShell, Windows Terminal and Command Prompt. There is no standalone `gtu.exe` (see [Troubleshooting](#troubleshooting)). Step-by-step checks for a clean machine are in [docs/install-checklist.md](docs/install-checklist.md).

**What each job needs:**

| | `review` | `tests` | `docs` |
|---|---|---|---|
| spare-gpu, Git, an API key | ✓ | ✓ | ✓ |
| BharatCode's CLI (Node.js, `npm.cmd install -g bharatcode`, `bharatcode.cmd auth login`) | not needed | **needed** | **needed** |

To install BharatCode's CLI: `winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-package-agreements --accept-source-agreements`, open a new PowerShell, then `npm.cmd install -g bharatcode` and `bharatcode.cmd auth login`. (`npm.cmd` and `bharatcode.cmd` rather than `npm` and `bharatcode`: they work even where PowerShell blocks scripts, which it does by default on many PCs.)

If the CLI is missing, `gtu add tests` / `gtu add docs` say so straight away, `gtu check` reports it, and `gtu start` leaves those jobs queued (not failed) and still runs review jobs.

```powershell
gtu demo [file]   # one short review, sent straight away, to see it work
```

### Your API key

```powershell
gtu setkey   # asks for the key (input hidden), tests it, saves it
gtu check    # says where it found the key and confirms BharatCode accepts it
```

`gtu setkey` saves the key in **Windows Credential Manager**: encrypted, readable only by your Windows account, and still there after you close the terminal or restart. It tests the key with one 1-token request first and won't save a key BharatCode rejects (`--no-check` skips the test). Run it again to replace the key; `gtu setkey --remove` deletes it.

Setting `$env:BHARATCODE_API_KEY = "..."` still works, but only lasts for that terminal; when it's set, it takes priority over the saved key.

`gtu check` reports whether BharatCode's CLI is installed and signed in, and where the key came from; then it calls `GET /models` and sends a 1-token `POST /chat/completions` to confirm the key is actually accepted (`/models` answers even with a wrong key). Exits non-zero if the key is missing, rejected, or BharatCode is busy; a missing CLI is reported but doesn't fail it, since review jobs don't need it.

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

## How the BharatCode integration works

`gtu integrate` registers spare-gpu as an MCP server in BharatCode's global config (`%APPDATA%\BharatCode\Config\opencode.jsonc`; it asks BharatCode's CLI for the exact location). Before changing the file it saves a copy next to it (`opencode.jsonc.spare-gpu-backup`); it edits the file in place, so your comments and other settings stay. If the file can't be edited safely (it doesn't parse, say), it changes nothing and prints the snippet and path to paste by hand. `bharatcode.cmd mcp list` should then show `✓ spare-gpu connected`.

A project-level `opencode.json` in a repo's root works too (commit it; an uncommitted file makes jobs refuse the repo). BharatCode ignores a file named `bharatcode.json`.

`gtu mcp` is the server itself; BharatCode starts it. Each of the seven tools is a thin wrapper over the same code as a `gtu` command (`gtu add`, `gtu list` + `gtu why`, `gtu report`, `gtu cancel`, `gtu advice`, `gtu cost`, `gtu run-when-free`), and they share the repo's `.gtu\gtu.db` safely with a running `gtu start`. No tool blocks the agent: queueing a job starts the background runner (`gtu start`, no window, output in `.gtu\runner.log`), and `spare_run_when_free` starts a small waiter per command (output in `.gtu\commands\<id>.log`). Commands for `spare_run_when_free` are checked in code against an allowlist of test, build and lint tools plus `bharatcode run`, never run through a shell, and refused if they contain shell characters; `GTU_RUN_ALLOWED` adds your own command prefixes.

## Troubleshooting

**`gtu` is "not recognized" after installing.** The terminal still has the PATH from before you installed. Close **every** PowerShell or Command Prompt window and open a new one (in VS Code, restart VS Code itself; its terminals keep the old PATH). If it still isn't found, run `py -m pipx ensurepath`, then open a new terminal again. pipx may suggest editing `~/.bashrc`; that's for Linux, ignore it. You can always run it by its full path: `& "$env:USERPROFILE\.local\bin\gtu.exe" --version`.

**"Python was not found; run without arguments to install from the Microsoft Store".** Python isn't installed; that message is a Windows shortcut, not Python. Do step 1 of [Get started](#get-started), then open a new terminal. If you installed Python but forgot "Add python.exe to PATH", use `py` instead of `python`; it works either way.

**"running scripts is disabled on this system" (for `npm` or `bharatcode`).** PowerShell blocks `.ps1` scripts by default on many PCs, and npm installs its commands as scripts. Use `npm.cmd` and `bharatcode.cmd` instead; they do the same thing and aren't blocked. gtu itself isn't affected.

**The API key doesn't stick between terminals.** `$env:BHARATCODE_API_KEY = "..."` only lasts for the window you typed it in. Run `gtu setkey` once instead: it saves the key in Windows Credential Manager for your account. `gtu check` shows where the key it's using came from. If `BHARATCODE_API_KEY` is set, it wins over the saved key; close that window or run `Remove-Item Env:BHARATCODE_API_KEY`.

**"BharatCode rejected your API key".** The key was mistyped, revoked or expired. Copy it again from [bharatcode.ai/account](https://bharatcode.ai/account) and run `gtu setkey`.

**"BharatCode's CLI isn't installed" or "not signed in".** Only `tests` and `docs` jobs (and the MCP route) need it. Install Node.js (`winget install --id OpenJS.NodeJS.LTS -e --source winget`), open a new terminal, then `npm.cmd install -g bharatcode` and `bharatcode.cmd auth login`. Queued tests and docs jobs wait for it and run on the next `gtu start`.

**"Credential store permissions or access could not be verified".** This error comes from BharatCode's own CLI, not from spare-gpu. On our test machine (CLI 1.15.46) it appeared, with or without spare-gpu, whenever the agent needed a second step, so tests and docs jobs and multi-step MCP requests stop there. `bharatcode.cmd auth status` still said "Signed in". We haven't found a fix yet; `bharatcode.cmd auth reconnect`, or `auth logout` then `auth login`, are the first things to try, and BharatCode's support should hear about it. Review jobs, `gtu demo` and `gtu check` don't use the CLI and aren't affected.

**Why is there no `gtu.exe` to download?** We built one, and Windows 11's Smart App Control blocked it on our own test machine ("An Application Control policy has blocked this file"), as it does with any program that isn't code-signed. SmartScreen warns about the same thing on Windows 10. A download most people can't open is worse than none, so pipx is the supported route until there's a signed build.

**`gtu start` seems to hang.** It's almost always waiting for spare capacity, on purpose. It prints why, e.g. `Gate closed: ... 2/2 slots busy, 7 queued. Next check in 60s`. To see the reason at any time, run `gtu why` in another terminal. To run now anyway, competing with everyone else's requests, stop it with Ctrl+C and run `gtu start --now`. `gtu stop` stops it from another terminal; jobs are paused, not lost.

**"the repo has uncommitted changes".** Jobs start from your last commit, so `gtu add` refuses a repo with uncommitted or untracked files, and a queued job can't start while there are some. Commit them (`git add -A` then `git commit -m "wip"`) or stash them (`git stash -u`).

**"paths longer than Windows allows".** A job works in a copy of your repo under `.gtu\worktrees\`, which makes every path about 25 characters longer. Move the repo to a shorter folder such as `C:\code\myrepo`, or turn on Windows long paths once (the error message shows the exact command, to run as Administrator).

**"could not remove its worktree ... holding a file".** Antivirus (usually Microsoft Defender) or an editor had a file in the job's folder open. gtu retries for a few seconds first. The job's results are already safe on its `gtu/<job-id>` branch; delete the folder it names when the program lets go.

**Saving output to a file.** `gtu report` already saves a copy under `.gtu\reports\` (UTF-8); use that. In Command Prompt, `gtu report > report.md` also writes clean UTF-8. Windows PowerShell 5.1 re-encodes anything you redirect or pipe (`>` saves UTF-16, and characters outside English can come out garbled), so prefer the saved copy there.

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
- Your API key comes from Windows Credential Manager (`gtu setkey`) or the `BHARATCODE_API_KEY` environment variable. It is never written to a file, logged, printed, or sent anywhere except to BharatCode, and it is removed from the environment of everything gtu starts: model-written tests, the BharatCode CLI, and the commands the CLI runs.

## Honest limitations

- The finding above is a single-user, single-night measurement, not a study of BharatCode's aggregate load. It shows the premise can be false; it doesn't show how often.
- The live stats endpoints (`/api/serving/usage`, `/api/serving/history`) are undocumented, unversioned, and could change or disappear without notice. The gate is built to fall back to our own signals when that happens.
- Measured queue waits vary a lot. BharatCode's reported wait (`x-bharatcode-queue-wait-ms`) was 0.3–12 s overnight on 30 Oct (03:10–05:10 IST), but 31–72 s between 08:10 and 08:50 IST. That gap isn't only time of day: at 05:30 IST (00:00 UTC) BharatCode cut the Qwen API lane from 4 slots to 2, so the overnight and morning numbers come from different capacity. These are early numbers: the full 24-hour gate-vs-send experiment is still running, and its results may change how the gate works.
- We assume the API lane's queue is the one our requests join. A free slot does not guarantee a prompt start: under pacing, BharatCode can hold a request for several seconds either way, and once answered busy (503) after holding it for 5.6 s.
- Two assumptions in the client are unconfirmed: that HTTP 429 should be treated like 503 (BharatCode's docs only mention 503), and that `Retry-After` is honoured when sent — it was never observed in testing.
- `gtu start` stops when the queue is empty; it does not wait for new jobs.
- Only the review job has run against the real service so far; tests and docs jobs are tested against fakes of BharatCode's API and CLI.
- Failing generated tests are dropped per test for Python (pytest); for other languages a whole test file is kept or discarded.
- A tests job only accepts new test files; it never edits your existing tests.
- A CLI step that times out is retried after the gate reopens; a task that always times out will keep retrying until its budget runs out.
- Only `/chat/completions` is used; `/responses` is not supported.
- This is a development-chores tool, not a general-purpose bulk prompt runner.
- The command is `gtu` and the package `gpu_token_utilization`, left over from an earlier project name.

## Research

- [research/livestats-endpoints.md](research/livestats-endpoints.md) — the live stats endpoints: what they return, how fresh, how trustworthy, the measurement behind the finding above, and the capacity gate design it led to.
- [research/token-overhead.md](research/token-overhead.md) — BharatCode CLI's fixed token overhead per step, measured: system prompt, built-in tool schemas, MCP tools and project context, with a script to reproduce it.
- [research/bharatcode-cli-scheduling-check.md](research/bharatcode-cli-scheduling-check.md) — whether BharatCode's own CLI and Desktop app already have a scheduling feature (checked so spare-gpu doesn't duplicate one that exists). Short answer: no.
- [scripts/livestats_log.py](scripts/livestats_log.py) — logs the live stats once a minute (Qwen and DeepSeek API lanes, plus totals) to CSV, to build a dataset of when BharatCode actually has spare capacity.
- [scripts/gate_vs_send.py](scripts/gate_vs_send.py) — the gate-vs-send experiment: every 20 minutes it records what the gate would decide, then sends one tiny request anyway and records BharatCode's reported queue wait. Results will be in `research/gate-vs-send-experiment.md`.

## Licence and attribution

MIT — see [LICENSE](LICENSE). This is an independent project, built for the BharatCode Build League, and is not affiliated with or endorsed by BharatCode.
