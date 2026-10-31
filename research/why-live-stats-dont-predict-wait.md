# Why BharatCode's live stats don't predict how long you'll wait

- **Dates:** measured 2026-10-30 03:07 IST to 2026-10-31 02:50 IST; docs checked 2026-10-31
- **Model and lane:** `qwen-3.8-27b`, `api` lane

**In short:** we measured how long BharatCode actually held our requests and compared that with what its live stats said at the moment of sending.

- With the lane's full 4 slots, the queue count tracks the wait reasonably well.
- With 2 slots, it tells you nothing.
- Our own recent waits didn't predict the next wait any better.

The waits themselves were short: a median of 13 s and a worst case of 80 s.

Throughout, **measured** marks what we observed directly. **Inferred** marks our interpretation.

## What the dashboard shows, and what it suggests

BharatCode publishes live load per model and lane at [bharatcode.ai/livestats](https://bharatcode.ai/livestats), backed by `GET /api/serving/usage`. For each lane it shows:

- `active_requests`;
- `queued_requests`;
- `slot_capacity`;
- `capacity_used_pct`.

To a user this reads like a traffic report: a full lane with a long queue means a long wait, and a free slot means you get in at once. spare-gpu was originally built on exactly that reading. It held work back until a slot was free.

## What we measured

**Data.** Every 20 minutes for about a day we read the live stats, then sent one tiny request whatever they said. We recorded the queue wait BharatCode reported for it. That gave:

- 40 paired samples; 39 have a live-stats reading, and 35 succeeded;
- plus two bursts of 5 sends.

Method and raw data: [gate-vs-send-experiment.md](gate-vs-send-experiment.md) and [data/gate-vs-send.csv](data/gate-vs-send.csv).

**Measured: the waits are short.** Over the 35 successful paired sends:

- median **13.2 s**;
- p90 56.3 s;
- worst **80.1 s**.

**Measured: the lane is almost always "full".** In 814 of 1,276 minute-by-minute readings, every slot was in use. In all 18 successful sends made while the lane had 2 slots, `capacity_used_pct` was 100%. Yet those requests were admitted, typically within seconds to a minute.

**Measured: how well each figure tracks the wait.** These are correlations between the figure at send time and the wait that request got.

| Figure at send time | All (n=35) | 4 slots (n=17) | 2 slots (n=18) |
|---|---|---|---|
| `queued_requests` | +0.47 | **+0.82** | **−0.02** |
| `capacity_used_pct` | +0.39 | +0.47 | none (always 100%) |
| `active_requests` | −0.18 | +0.47 | none (always 2) |
| all lanes' total queued | −0.00 | | |

Two sends at 2 slots show the problem:

| Sent (IST) | Lane at send time | Wait |
|---|---|---|
| 30 Oct 09:10 | 2/2 busy, **7 queued** | **3.5 s** |
| 30 Oct 07:46 | 2/2 busy, **3 queued** | **80.1 s** |

**Measured: our own recent waits don't predict the next one better.**

[scripts/validate_wait_predictor.py](../scripts/validate_wait_predictor.py) compares three predictors of each paired send's wait, all known before sending:

- BharatCode's queue count;
- its capacity percentage;
- the median wait of our last 3 requests, if no more than 90 minutes old.

The error is the average miss in seconds. The two stats are in different units from the wait, so each is turned into seconds by a line fitted with that sample left out. The baseline just guesses the usual wait and knows nothing about the moment.

| Predictor | 4 slots: correlation | 4 slots: error | 2 slots: correlation | 2 slots: error |
|---|---|---|---|---|
| baseline (the usual wait) | | 10.5 s | | **15.9 s** |
| `queued_requests` | **+0.80** | **8.4 s** | −0.09 | 19.6 s |
| `capacity_used_pct` | +0.45 | 10.6 s | none | 17.7 s |
| our last 3 waits, as is | +0.65 | 11.7 s | +0.28 | 18.9 s |
| our last 3 waits, fitted | | 10.1 s | | 17.5 s |

These are the 30 sends (15 per slot count) that had 3 earlier waits within 90 minutes. Other settings don't change the picture: the last 1, 3 or 5 requests, no more than 30 to 120 minutes old.

- **With 4 slots,** BharatCode's queue count is the best predictor.
- **With 2 slots,** nothing beats the know-nothing baseline, our own measurements included.

## Why: account-level pacing (inferred)

On 30 Oct 2026, BharatCode's founder posted this in BharatCode's Discord/community group. It's a community announcement, not official documentation; "FUP" is the fair use policy:

> "Ok, FUP changes are now live. These changes add pacing to requests on an account level, freeing up space for low capacity users. I am still tweaking some parameters and thats why you might see a queue forming. If you have any automated timeout and retry mechanism set, please raise the limits."

**Our interpretation:** if requests are paced per account, the wait one account sees depends mostly on that account's own pacing state. It depends much less on the lane-wide queue the dashboard shows. That fits what we measured, especially the 2-slot morning, where the visible queue carried no information. But we didn't observe the pacing mechanism itself. "Pacing dominates" is an inference from the announcement plus our data.

We also can't compare before and after. The announcement is dated 30 Oct and our measurements start at 03:07 IST on 30 Oct, and we don't know at what time the change went live.

## The header that does report the real wait (measured)

All 44 successful chat completions in the experiment came back with three response headers:

- `x-bharatcode-queue-wait-ms`: how long the request waited before a slot took it;
- `x-bharatcode-scheduler-requested-at-ms`;
- `x-bharatcode-scheduler-admitted-at-ms`.

In every one of them, `admitted − requested` equals `queue-wait-ms` exactly.

**Not in the public docs, as far as we can tell.** BharatCode's API page ([bharatcode.ai/build](https://bharatcode.ai/build), read 31 Oct 2026):

- documents `x-request-id` ("Include the x-request-id response header when reporting a problem");
- says "Requests share capacity and may queue";
- says "A `503` means the model is busy. Wait a few seconds and retry".

It doesn't mention the queue-wait headers. Being undocumented, they could change or disappear without notice.

## What this means if you build on BharatCode's API

1. **Don't time your requests from the live stats.** A full lane doesn't mean a long wait. With 2 slots, the queue count told us nothing at all. Holding work until a slot looked free would have cost spare-gpu hours to save seconds: at least 4,353 minutes against 12.3 ([gate-vs-send-experiment.md](gate-vs-send-experiment.md)).
2. **Send, and handle busy answers.** Do what the docs say for a `503` (wait, retry), and follow the founder's advice to raise timeouts. Our worst wait was 80 s, and one request timed out at our 180 s limit.
3. **Log `x-bharatcode-queue-wait-ms`.** It's the only figure we found that reports the wait you actually got. Treat it as a record of what happened, not a forecast: in our data, recent waits didn't predict the next one.
4. **Expect half capacity in the mornings.** The Qwen API lane ran 2 slots from 05:30 to 13:30 IST on both days we logged, and 4 otherwise. Median waits were 18.0 s at 2 slots and 8.1 s at 4. That comparison is confounded with time of day (see below).

## What spare-gpu does with this

- **It sends right away.** It backs off on `503`s and timeouts, and pauses after repeated busy answers. Waiting for a free slot is opt-in (`gtu start --wait-for-capacity`).
- **`spare_capacity_advice` / `gtu advice` describe instead of forecasting.** They report:
  - the median of our last few measured waits, with how many and how old;
  - whether waits are rising or falling over the last 20 minutes;
  - what's usual at this hour from our own history;
  - BharatCode's live stats, labelled as context.

  They say "not enough recent data" rather than guess.

## Limitations

- **One account, one machine, one lane** (`qwen-3.8-27b` API), about 2 days. Other accounts, under different pacing, may see different waits.
- **Gaps.** The laptop slept from 13:38 to 22:46 on 30 Oct and for shorter spells, so there's no afternoon or early-evening data. The scheduled 15:00 burst ran at 22:46 instead.
- **Hour of day and slot count are confounded.** 2-slot sends were all in the morning and 4-slot sends all at night, so "2 slots" and "morning" can't be told apart.
- **Small samples.** There are 35 successful paired sends, 15 to 18 per slot count. The correlations have wide uncertainty: with n = 17, the 95% interval for r = 0.82 runs from about 0.56 to 0.93.
- **Requests 20 minutes apart.** A real job sends requests seconds apart, and at that spacing the most recent wait might predict the next much better. The only closely spaced data is two bursts of 5, too little to say.
- **Pacing was still being tuned** ("I am still tweaking some parameters"), so these numbers describe 30 to 31 Oct, not necessarily today.
- **Tiny requests.** Every request was about 21 tokens, and we assume the wait to get admitted doesn't depend on request size. We didn't test that.

## Reproducing

```powershell
python scripts\analyse_gate_vs_send.py research\data\gate-vs-send.csv research\data\qwen-api-livestats.csv
python scripts\validate_wait_predictor.py research\data\gate-vs-send.csv 3 90
```
