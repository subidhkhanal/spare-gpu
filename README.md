# spare-gpu

**A harness for the repo work you keep putting off — tests, docs, code review. Queue it in plain words from BharatCode's CLI and it runs unattended: each job on its own git branch, generated tests kept only if they pass, resumable after a crash, capped by a token budget, with every token accounted for.**

[![tests](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml/badge.svg)](https://github.com/subidhkhanal/spare-gpu/actions/workflows/test.yml)

```
you: "queue tests for the payments module"
BharatCode: queued job 3f9a1c2e. Results go on branch gtu/3f9a1c2e; your branch isn't touched.
```

## The finding

**BharatCode's waits are short, and its live stats don't predict them.**

- **Waits are short.** We sent 35 successful paired requests to BharatCode. The median wait was **13 s** and the worst was **80 s**.
- **With all 4 slots,** the dashboard's queue count tracks the wait reasonably (r = 0.80).
- **At half capacity (2 slots),** it predicts nothing (r = −0.09). Neither did our own recent measurements.

So spare-gpu doesn't try to time the load. It sends, handles busy answers with back-off, and shows what it measured rather than forecasting.

**This is a change of mind.** spare-gpu was first built to hold work until BharatCode's live stats showed a free slot. Measuring that showed it was wrong. Across 39 samples, sending straight away cost **12.3 minutes** of queue wait in total. Waiting for a free slot would have cost **at least 4,353 minutes**. On 30 Oct the gate would never have opened from 07:46 to 13:38, so an "overnight" job wouldn't have started. Waiting for capacity is now opt-in (`gtu start --wait-for-capacity`).

The data shows *what*. For *why*, BharatCode added account-level pacing on 30 Oct. We infer that pacing, not the lane-wide queue the dashboard shows, decides how long you wait. The full write-up separates what we measured from what we infer, and covers the undocumented `x-bharatcode-queue-wait-ms` header that reports the real wait: [research/why-live-stats-dont-predict-wait.md](research/why-live-stats-dont-predict-wait.md).

## Set up once

Install BharatCode's CLI if you haven't already (`npm.cmd install -g bharatcode`, `bharatcode.cmd auth login`), then:

```powershell
pipx install git+https://github.com/subidhkhanal/spare-gpu
gtu setkey        # save your BharatCode API key
gtu integrate     # registers spare-gpu as an MCP server inside BharatCode
```

`gtu integrate` finds BharatCode's config, backs it up, adds one entry, and confirms `bharatcode.cmd mcp list` shows `✓ spare-gpu connected`. Undo it any time with `gtu integrate --uninstall`.

## Then just ask, inside BharatCode

| You say | What happens |
|---|---|
| "queue tests for the payments module", "write docs for src/utils overnight" | Queues the job and starts a background runner that keeps going after you close BharatCode |
| "what's queued?", "is my tests job done?" | Queued, running and finished work |
| "what did spare-gpu do last night?" | What ran, what it produced, what it cost |
| "cancel the docs job" | Cancels it |
| "is now a good time for a big refactor?" | How long our last requests actually waited, whether that's rising, and what's usual at this hour; it says so when there isn't enough data |
| "how much of that was overhead?" | Tokens per job, split into CLI overhead and the actual task |
| "run the test suite when BharatCode is free" | Runs a test, build or lint command (or `bharatcode run`) once BharatCode shows a free slot |

Every job lands on its own git branch (`gtu/<job-id>`). Your checkout is only ever read, and nothing touches `main` until you say so.

## What it will never do

- Never touch your `main` branch or working checkout. All work happens on its own branch.
- Never push to GitHub on its own.
- Never run anything but allowlisted test, build and lint commands, and always under a timeout.
- Never log, print or send your API key anywhere but BharatCode. It lives in Windows Credential Manager.

## Honest limitations

- **Windows only.** Built and tested on Windows 10 and 11; CI runs on Windows.
- **The wait figures rest on a small experiment.** One account, one model lane, about two days, with gaps while the laptop slept. Hour of day and slot count can't be separated in it.
- **BharatCode's CLI sometimes fails partway through a run** with "Credential store permissions or access could not be verified". This happens without spare-gpu too. spare-gpu continues the same session (up to 8 times), which gets jobs through but costs extra tokens.
- **The live-stats endpoint and the queue-wait header are undocumented.** They could change without notice.
- **This is a dev-chores tool,** not a general-purpose bulk prompt runner.

Research: [why live stats don't predict wait](research/why-live-stats-dont-predict-wait.md), [the gate-vs-send experiment](research/gate-vs-send-experiment.md), [the live-stats endpoints](research/livestats-endpoints.md), [token overhead](research/token-overhead.md).

## Licence and attribution

MIT — see [LICENSE](LICENSE). Independent project, built for the BharatCode Build League. Not affiliated with or endorsed by BharatCode.
