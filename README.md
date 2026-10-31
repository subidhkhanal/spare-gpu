# spare-gpu

**An MCP server for BharatCode that runs your repo chores on BharatCode's spare GPU capacity — tests, docs, code review — overnight, unattended, just by asking.**

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

```
you: "queue tests for the payments module, run them overnight"
BharatCode: queued. I'll watch for spare capacity and let you know.
```

No dashboards, no cron, no second CLI to learn. You talk to BharatCode like you already do — spare-gpu is a tool it can reach for.

## Why this exists

We measured BharatCode's live load before assuming "run it overnight" was safe. At **01:30 IST**, the Qwen 3.8 27B lane was running all 4 slots with **13 requests queued** — 100% capacity, in the middle of the night ([raw data](research/livestats-endpoints.md)).

A fixed schedule fires blindly into that. spare-gpu doesn't: it watches BharatCode's real-time capacity and only sends work when there's an actual free slot — then backs off the instant BharatCode says busy.

## Set up once

Install BharatCode's CLI if you haven't already (`npm.cmd install -g bharatcode`, `bharatcode.cmd auth login`), then:

```powershell
pipx install git+https://github.com/subidhkhanal/spare-gpu
gtu setkey        # save your BharatCode API key
gtu integrate      # registers spare-gpu as an MCP server inside BharatCode
```

`gtu integrate` finds BharatCode's config, backs it up, adds one entry, and confirms `bharatcode.cmd mcp list` shows `✓ spare-gpu connected`. Undo any time with `gtu integrate --uninstall`.

## Then just ask, inside BharatCode

| You say | What happens |
|---|---|
| "queue tests for the payments module", "write docs for src/utils overnight" | Queues the job and starts a background runner that keeps going after you close BharatCode |
| "what's queued?", "why hasn't my job started?" | Queued, running and finished work, and what it's waiting for |
| "what did spare-gpu do last night?" | What ran, what it produced, what it cost |
| "cancel the docs job" | Cancels it |
| "is now a good time for a big refactor?" | BharatCode's load right now and its quietest hours this week |
| "how much of that was overhead?" | Tokens per job, split into CLI overhead vs. actual task |
| "run the test suite when BharatCode is free" | Runs a test/build/lint command (or `bharatcode run`) once there's spare capacity |

Every job lands on its own git branch (`gtu/<job-id>`) — your checkout is only ever read, nothing touches `main` until you say so.

## What it will never do

- Never touch your `main` branch or working checkout — all work happens on its own branch.
- Never push to GitHub on its own.
- Never run anything but allowlisted test/build/lint commands, under a timeout.
- Your API key lives in Windows Credential Manager, never logged, printed, or sent anywhere but BharatCode.

## Honest limitations

- Windows-first: built and tested on Windows 10/11; CI runs Windows only.
- The capacity reading comes from BharatCode's undocumented live-stats endpoint — unversioned, could change without notice. spare-gpu falls back to its own request history if it does.
- Only the review job has run end-to-end against the real BharatCode service so far; tests and docs jobs are tested against fakes of BharatCode's API and CLI.
- This is a dev-chores tool, not a general-purpose bulk prompt runner.

Full research behind the capacity gate: [research/livestats-endpoints.md](research/livestats-endpoints.md), [research/gate-vs-send-experiment.md](research/gate-vs-send-experiment.md), [research/token-overhead.md](research/token-overhead.md).

## Licence and attribution

MIT — see [LICENSE](LICENSE). Independent project, built for the BharatCode Build League. Not affiliated with or endorsed by BharatCode.
