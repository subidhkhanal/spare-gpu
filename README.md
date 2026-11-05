# spare-gpu

**A harness for the repo work you keep putting off — tests, docs, code review — inside BharatCode Desktop. Ask in plain words and it runs as its own background Desktop session: each job on its own git branch, generated tests kept only if they pass, resumable after a crash, capped by a token budget, with every token accounted for. And when DeepSeek has a queue, it can hand a task that Qwen does just as well to Qwen, so you don't wait.**

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

What spare-gpu *does* use is measured waits, to choose **which model** runs a task, not **when** to send it. BharatCode now publishes measured wait percentiles per model (`/api/serving/queue-waits`); on 4 Nov at 14:12 UTC DeepSeek's median wait over the last hour was **85 s** (p75 149 s) against Qwen's **3 ms**. For task types where Qwen's results are measured to be no worse, spare-gpu runs them on whichever model starts sooner and checks the result before keeping it (see *Wait-aware routing* below).

The data shows *what*. For *why*, BharatCode added account-level pacing on 30 Oct. We infer that pacing, not the lane-wide queue the dashboard shows, decides how long you wait. The full write-up separates what we measured from what we infer, and covers the undocumented `x-bharatcode-queue-wait-ms` header that reports the real wait: [research/why-live-stats-dont-predict-wait.md](research/why-live-stats-dont-predict-wait.md).

## Set up once

With BharatCode Desktop installed and signed in:

```powershell
pipx install git+https://github.com/subidhkhanal/spare-gpu
gtu integrate     # registers spare-gpu in every BharatCode Desktop on this machine
```

Then restart BharatCode Desktop. No API key is needed: jobs run through Desktop's own server and sign-in.

`gtu integrate` registers spare-gpu as an MCP server in BharatCode's config, backing each file up first. BharatCode keeps one config per release channel: the released Desktop app (and the CLI) share one; Beta, Dev and Local builds each have their own. A Desktop built from source runs as Dev. By default it registers in all of them; `--channel stable|beta|dev|local` picks one. Undo with `gtu integrate --uninstall`.

## Then just ask, inside BharatCode Desktop

| You say | What happens |
|---|---|
| "queue tests for the payments module", "write docs for src/utils" | Queues the job and starts it as a background Desktop session named "spare-gpu: …", which you can open and watch |
| "write tests for src/pay.py on whichever model is free", "get this reviewed now, don't wait in the queue" | `spare_delegate`: hands the task to the model that will start it sooner, if that's safe for the task type; returns the branch and why that model |
| "how long is DeepSeek's queue?", "is now a good time for a big refactor?" | `spare_waits`: each model's measured waits, free slots, your own recent waits and what's usual at this hour, then which would start sooner |
| "what's queued?", "is my tests job done?" | Queued, running and finished work, and which model each job runs on |
| "what did spare-gpu do last night?", "what did it cost?" | What ran, what it produced, its tokens, and how much of tonight's token budget is used |
| "cancel the docs job" | Cancels it |
| "run the test suite when BharatCode is free" | Runs a test, build or lint command once BharatCode shows a free slot |

### `/auto`

`gtu integrate` also adds a `/auto` slash command to Desktop. Type `/auto write tests for src/pay.py` (or docs, or a review) and the task goes straight to `spare_delegate`: it runs on DeepSeek V4.1 Flash unless the other model would start it sooner and the task type is switch-safe. `/auto` alone shows the current waits. The handoff itself runs on Qwen 3.8 27B, so a stalled DeepSeek queue can't hold up the handoff that's meant to avoid it. The command is `command/auto.md` in Desktop's config folder. Edit it freely: once its "Installed by spare-gpu" line is gone, spare-gpu never overwrites or removes it. `gtu integrate --uninstall` removes the unedited one.

Every job lands on its own git branch (`gtu/<job-id>`). Your checkout is only ever read, and nothing touches `main` until you say so.

For one job's token detail (input, output, cached, reasoning, and a breakdown by message type), open its "spare-gpu: …" session in Desktop: Desktop measures that itself. spare-gpu adds the totals across jobs and nights, and per-job and per-night token budgets that stop a job rather than just report it.

## Wait-aware routing

BharatCode Desktop only offers its own models, so spare-gpu can't change the model of your chat. It routes the work it runs itself: each job's Desktop session is started on the model chosen for it.

- **Only switch-safe task types move.** A type is switch-safe when [scripts/eval_models.py](scripts/eval_models.py) has run the same tasks on both models and Qwen's checked results are no worse than DeepSeek's (95% paired-bootstrap interval above −5% of DeepSeek's mean). The first eval (5 Nov 2026, 3 tasks per type, every job allowed to finish) gave this result:
  - **docs: switch-safe.** Qwen matched DeepSeek on every task.
  - **review: stays on DeepSeek.** Qwen scored about 29% lower.
  - **tests: stays on DeepSeek for now.** The averages were almost equal (32.0 vs 32.7 passing tests kept), but 3 tasks vary too much to prove Qwen no worse.

  The verdicts are in [routing_policy.json](src/gpu_token_utilization/routing_policy.json) and the raw scores in [research/eval/](research/eval/). To override it for yourself, list the types, one per line, in your project's `.gtu/switch-safe` file (read on every decision; no restart needed), or set `GTU_SWITCH_SAFE=tests,docs`.
- **Only models your plan includes:** BharatCode lists every model, but a Lite plan only includes Qwen. When BharatCode refuses a model for your plan, spare-gpu remembers that, reruns the step on the model your plan does include, and never routes to the refused one again.
- **Only when it's worth it:** the other model must save at least 20 s on the estimate (`GTU_ROUTE_MIN_SAVING`), and its lane must have a free slot with nothing queued, so routed work never piles into a queue.
- **Sticky per job:** a job keeps its model for every step, with two readings in a row needed before routing back.
- **Checked, with a fallback:** if a routed job's result keeps nothing after the usual checks (passing tests, docs-only changes), it reruns once on the preferred model.

The estimate per model blends BharatCode's measured wait percentiles with your own recent waits on that model (which capture account pacing) and falls back to what's usual at this hour. It never claims a number it doesn't have.

## What it will never do

- Never touch your `main` branch or working checkout. All work happens on its own branch.
- Never push to GitHub on its own.
- Never run anything but allowlisted test, build and lint commands, and always under a timeout.
- Never use your Claude Code skills or spare-gpu's own tools inside a job: the job's permission file denies them, which also keeps ~3.5k tokens of skill listings out of every step.
- Never log, print or send an API key anywhere but BharatCode (only needed outside Desktop; it lives in Windows Credential Manager).

## Honest limitations

- **Windows only.** Built and tested on Windows 10 and 11; CI runs on Windows.
- **Jobs run while BharatCode Desktop is open.** They are Desktop sessions on Desktop's own server; a job interrupted by closing Desktop resumes the next time spare-gpu's runner starts.
- **Routing is only as good as its evaluation.** The shipped policy routes nothing until the evaluation has been run; the starter task set in [research/eval/tasks.json](research/eval/tasks.json) is far smaller than the 30–50 tasks per type a firm decision needs.
- **The wait figures rest on a small experiment.** One account, one model lane, about two days, with gaps while the laptop slept. Hour of day and slot count can't be separated in it.
- **BharatCode's live-stats, queue-waits and queue-wait header are undocumented.** They could change without notice; spare-gpu treats a failed or stale reading as "no data", never as "no wait".
- **This is a dev-chores tool,** not a general-purpose bulk prompt runner.

Research: [why live stats don't predict wait](research/why-live-stats-dont-predict-wait.md), [the gate-vs-send experiment](research/gate-vs-send-experiment.md), [the live-stats endpoints](research/livestats-endpoints.md), [token overhead](research/token-overhead.md).

## Licence and attribution

MIT — see [LICENSE](LICENSE). Independent project, built for the BharatCode Build League. Not affiliated with or endorsed by BharatCode.
