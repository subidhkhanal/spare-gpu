# Does waiting for a free slot beat just sending?

**Short answer: no.** Waiting for the gate would have cost at least **4,353 minutes** of delay across 40 samples. Sending straight away cost **12.3 minutes** of queue time, plus 4 failed requests. Waits sent into a full lane are short: median 13 s, worst 80 s. The gate's signal ("is a slot free?") stays at "no" for hours, even while requests are getting in within seconds.

- **Dates:** sends 2026-10-30 03:07 IST to 2026-10-31 02:50 IST; live stats 2026-10-30 01:47 IST to 2026-10-31 11:28 IST
- **Model and lane:** `qwen-3.8-27b`, `api` lane
- **Scripts:**
  - [scripts/gate_vs_send.py](../scripts/gate_vs_send.py) does the sends.
  - [scripts/livestats_log.py](../scripts/livestats_log.py) logs the live stats once a minute.
  - [scripts/analyse_gate_vs_send.py](../scripts/analyse_gate_vs_send.py) produces every number below.
- **Raw data:**
  - [data/gate-vs-send.csv](data/gate-vs-send.csv): one row per send.
  - [data/qwen-api-livestats.csv](data/qwen-api-livestats.csv): BharatCode's live stats, once a minute.
- **Cost:** 50 requests of about 21 tokens each, about 1,000 tokens in all.

## Question

spare-gpu's gate holds work back until BharatCode's live stats show a free slot on the model's lane. That is worth doing only if two things hold:

1. what the live stats show predicts the wait a request actually gets;
2. the time spent waiting for the gate is less than the time lost by sending into load.

## Method

**Paired samples.** Every 20 minutes the script:

1. read `/api/serving/usage` and recorded what the gate would decide;
2. sent one tiny chat request **regardless of the decision**;
3. recorded the queue wait BharatCode reports for that request (`x-bharatcode-queue-wait-ms`), plus the status and any error.

There were 40 paired samples. Sends were never retried, so a busy reply counts as a failure.

**Bursts.** Two bursts of 5 back-to-back sends. One ran at 03:07 on 30 Oct. The other was scheduled for 15:00 that day, but the laptop was asleep, so Task Scheduler ran it when the laptop woke at **22:46**. There is no daytime burst.

**Gate replay.** The once-a-minute live-stats log was replayed through spare-gpu's shipped gate rule from `gate.py`:

- A reading is **quiet** when the lane is up, a slot is free, and no more than 5 requests are queued.
- The gate **opens** after 3 quiet readings in a row and **closes** after 2 busy ones.

That gives, for any minute, how long a job arriving then would have waited for the gate. Where the log stops (laptop asleep) before the gate opens, the figure is a lower bound, marked "at least".

## Results

### 1. The queue wait, sent straight into the lane

All 35 successful paired sends:

| | Sends | Median | p90 | Max |
|---|---|---|---|---|
| **All** | 35 | **13.2 s** | 56.3 s | 80.1 s |
| Gate said quiet | 6 | 0.0 s | 0.0 s | 0.0 s |
| Gate said busy | 29 | 14.9 s | 61.6 s | 80.1 s |
| 4 slots | 17 | 8.1 s | 24.8 s | 60.3 s |
| 2 slots | 18 | 18.0 s | 68.4 s | 80.1 s |

By hour of day (IST):

| Hour | Slots | Sends | Median | Max | Failures |
|---|---|---|---|---|---|
| 00 | 4 | 3 | 0.0 s | 0.0 s | 0 |
| 01 | 4 | 2 | 8.5 s | 17.1 s | 1 |
| 02 | 4 | 2 | 0.0 s | 0.0 s | 1 |
| 03 | 4 | 2 | 2.4 s | 4.6 s | 1 |
| 04 | 4 | 3 | 9.8 s | 10.7 s | 0 |
| 05 | 4 → 2 | 2 | 9.8 s | 11.5 s | 1 |
| 07 | 2 | 2 | 45.9 s | 80.1 s | 0 |
| 08 | 2 | 3 | 50.4 s | 72.1 s | 0 |
| 09 | 2 | 3 | 14.9 s | 37.9 s | 0 |
| 10 | 2 | 3 | 43.9 s | 48.3 s | 0 |
| 11 | 2 | 2 | 42.4 s | 66.9 s | 1 |
| 12 | 2 | 3 | 13.2 s | 14.2 s | 0 |
| 13 | 2 → 4 | 2 | 16.0 s | 18.2 s | 0 |
| 23 | 4 | 3 | 33.8 s | 60.3 s | 0 |

The afternoon and early evening (14:00 to 22:00) are missing, because the laptop was asleep.

**Hour and slot count can't be separated in this data.** The lane has 2 slots from 05:30 to 13:30 IST and 4 at all other times. Every 2-slot send was in the morning and every 4-slot send was at night. So "mornings are slower" and "2 slots are slower" are the same observation here. The only overlap is the two changeover hours, with one send on each side of each change: 05:10 at 4 slots waited 8.1 s and 05:30 at 2 slots waited 11.5 s; 13:10 at 2 slots waited 18.2 s and 13:30 at 4 slots waited 13.7 s. That's too few to separate the effects.

**Failures.** There were 5 failed paired sends:

- three `503 busy` replies (03:30 on 30 Oct, then 01:50 and 02:10 on 31 Oct);
- one 180-second timeout (11:30 on 30 Oct);
- one network failure at 05:54 on 30 Oct, as the laptop went to sleep. This one isn't counted against BharatCode.

All four BharatCode failures happened when the gate said busy. The real client retries a `503` with a back-off of 5, 10, 20 and 40 s, so a failure like these costs tens of seconds, not hours.

### 2. Do the waits track what the live stats show?

Correlation with the queue wait, across the 35 successful paired sends:

| Live-stats figure | Pearson | Spearman |
|---|---|---|
| this lane's `queued_requests` | **+0.47** | **+0.61** |
| this lane's `active_requests` | -0.18 | -0.14 |
| this lane's slot count | -0.44 | -0.52 |
| all lanes' total queued | -0.00 | -0.02 |
| all lanes' total active | -0.05 | -0.10 |
| `queued_requests`, **4-slot sends only** (n=17) | **+0.82** | +0.76 |
| `queued_requests`, **2-slot sends only** (n=18) | **-0.02** | -0.05 |

- **`active_requests` carries no information.** Whenever the lane is busy it reads the full slot count: 814 of the 1,276 logged minutes showed every slot in use. That is the figure the gate's "free slot" test is built on.
- **The queue partly predicts the wait at 4 slots, and not at all at 2.** At 2 slots, a send with 7 queued waited 3.5 s, while one with 3 queued waited 80 s. In the morning the wait is effectively unpredictable from the stats, which points to account pacing or something else we can't see deciding it.
- **Site-wide totals tell you nothing.**

### 3. How long the gate stays closed

From the replayed gate, the longest stretches it would have stayed closed:

| Closed from (IST) | to | Length |
|---|---|---|
| 30 Oct 07:46 | 13:38 | **at least 352 min** (log stops; never opened) |
| 30 Oct 01:47 | 05:38 | **at least 231 min** (log stops; never opened) |
| 30 Oct 22:46 | 31 Oct 00:05 | 79 min |
| 31 Oct 01:44 | 02:28 | 44 min |
| 31 Oct 09:12 | 09:38 | 26 min |
| 31 Oct 05:50 | 06:11 | at least 21 min |
| 31 Oct 01:03 | 01:20 | 17 min |
| 31 Oct 05:12 | 05:27 | 15 min |

The gate was open in 424 of the 1,292 logged minutes (33%).

**Wait for the gate if a job arrived at a random minute, by hour:**

| Hour (IST) | 30 Oct: open at once | 30 Oct: median wait | 31 Oct: open at once | 31 Oct: median wait | 31 Oct: max wait |
|---|---|---|---|---|---|
| 00 | | | 82% | 0 min | 5 min |
| 01 | 0% | at least 225 min | 45% | 4 min | 44 min |
| 02 | 0% | at least 188 min | 53% | 0 min | 28 min |
| 03 | 0% | at least 128 min | 100% | 0 min | 0 min |
| 04 | 0% | at least 68 min | 95% | 0 min | 13 min |
| 05 | 0% | at least 18 min | 28% | at least 5 min | at least 21 min |
| 07 | 0% | at least 346 min | 65% | 0 min | 11 min |
| 08 | 0% | at least 308 min | 95% | 0 min | 10 min |
| 09 | 0% | at least 248 min | 40% | 2 min | 26 min |
| 10 | 0% | at least 188 min | 88% | 0 min | 15 min |
| 11 | 0% | at least 128 min | 62% | 0 min | 8 min |
| 12 | 0% | at least 68 min | | | |
| 13 | 0% | at least 19 min | | | |
| 22 | 0% | 73 min | | | |
| 23 | 0% | 36 min | | | |

**Two consecutive days looked completely different.** On 30 Oct the gate wouldn't have opened for anything between 01:47 and 05:38, or between 07:46 and 13:38. A job queued at 02:00 "to run overnight" would still not have started at dawn. On 31 Oct the gate was open most of the time. The same hour can mean "open now" or "closed for 4 hours", and nothing in the stats says in advance which.

The gate's shipped settings aren't what causes this. Replaying with a looser rule (open after a single quiet reading) gives almost the same picture: closed 07:46 to 13:34 (348 min) and 02:42 to 05:38 (at least 176 min) on 30 Oct. The one real gated run, on 30 Oct with the single-reading rule, waited 11 minutes. Its own polling caught a slot that was free for a moment at about 02:53, which the once-a-minute log never saw: only 12 of the 234 logged minutes before 06:00 that morning showed a free slot. So the replay may slightly overstate how long the gate stays shut. It can't explain a difference of hours against seconds.

### 4. The decisive comparison: waiting for the gate vs sending into load

For each paired sample, this compares the queue wait the send actually got with how long the gate would have held it.

| Night | Samples | Sent now: queue wait | Sent now: failures | Waiting for the gate |
|---|---|---|---|---|
| 30 Oct 03:10 to 13:30 | 27 | 10.1 min in all | 2 | **at least 4,180 min** in all (at least 8 min each, median 128 min) |
| 30 Oct 23:10 to 31 Oct 02:50 | 12 | 2.2 min in all | 2 | **173 min** in all (median 6 min, max 55 min) |
| **Total** | **39** | **12.3 min** | **4** | **at least 4,353 min** |

The 05:54 sample on 30 Oct is excluded because it had no live-stats reading.

Even on the quieter night, the one where the gate's verdict looked best, it cost 173 minutes to save 2.2 minutes of queue time and two retries.

**The earlier single run is confirmed, not contradicted.** It saw 11 minutes of waiting for the gate against about 15 s of queue wait. The full data has the same shape, and usually worse: waits for the gate are measured in tens of minutes to hours, queue waits in seconds to about a minute. This holds in every hour that has data. Hour of day changes how big the gap is, but it never changes which way it points.

### 5. Do our own bursts get paced harder?

| Burst | Lane at the time | Waits, in order | Failures | Nearest paired send |
|---|---|---|---|---|
| 03:07, 30 Oct | 4/4 busy, 2–4 queued | 6.2, 3.6, 3.2, 6.9, 11.8 s | none | 03:10: 4.6 s (2 queued) |
| 22:46, 30 Oct | 4/4 busy, 5–6 queued | 71.9 s, `503`, 27.4, 55.5, 63.4 s | one `503` | 23:10: 18.8 s, 23:30: 60.3 s (6–7 queued) |

There is **no clear sign that our own back-to-back use is throttled.**

- The 03:07 burst stayed in single-digit seconds until its fifth send (11.8 s), when the visible queue also grew from 2 to 4.
- The 22:46 burst's *first* request, sent before we had any other traffic, already waited 72 s. Later sends didn't get steadily worse (27 s, then 55 s, then 63 s), and the paired sends nearby on the same busy lane waited 19 to 60 s.

Two bursts of five are too few to rule out throttling on a sustained, hours-long job, and there's no daytime burst. That is the one open question that could still justify a gate (see Limits).

### 6. Capacity changes and data gaps

**Slots on the Qwen API lane:**

| From (IST) | Slots |
|---|---|
| 30 Oct 01:47 (log start) | 4 |
| 30 Oct 05:30 | **2** |
| 30 Oct 13:30 | 4 |
| 31 Oct 05:30 | **2** |

It's a daily schedule: **2 slots from 05:30 to 13:30 IST (00:00 to 08:00 UTC), 4 otherwise.** The lane was never marked unavailable, and 16 of the 1,292 live-stats fetches failed.

**Gaps in the log (laptop asleep or offline):**

| From (IST) | To | Length |
|---|---|---|
| 30 Oct 05:38 | 06:09 | 31 min (two gaps) |
| 30 Oct 06:09 | 07:46 | 97 min |
| 30 Oct 13:38 | 22:46 | **548 min** (the whole afternoon and evening) |
| 31 Oct 06:11 | 07:15 | 64 min (three gaps) |

The paired-send task got 40 of the 72 samples it was scheduled for.

## Limits

- **Coverage.** The afternoon and early evening (14:00 to 22:00 IST) have no data at all, and there are only 40 paired samples over two different nights.
- **The two nights differed a lot.** Two days isn't enough to say which one is typical.
- **Sustained use wasn't tested.** We sent single tiny requests and two short bursts. A long job makes many large requests in a row. If BharatCode paces an account harder after minutes of continuous use, the waits per step could be longer than measured here. We have no evidence of that, but we haven't ruled it out either.
- **Courtesy isn't measured.** Sending into a queue adds one request to everyone else's wait. This experiment measures what it costs *us*, not what it costs other users.
- **One lane only:** `qwen-3.8-27b` on the API lane.
- **The wait figure is BharatCode's own header.** It's undocumented and could change meaning without notice.
- **Minute-level sampling.** The log may miss slots that come free for only a few seconds (see section 3). That makes the replayed waits for the gate an overestimate by minutes, not by hours.

## Decision

The three options compared:

| Option | When it's the right call | What the data says |
|---|---|---|
| Keep the gate as is | waits sometimes get long, and they track the visible stats | Waits never went past 80 s. The gate's own input, `active_requests`, is pinned at full while requests still get in. **No.** |
| Make it wait-aware (back off on measured queue wait) | waits are sometimes painful | Waits sometimes run 1 to 2 minutes, never longer. Any threshold worth having (say 2 min) wouldn't have triggered once in 44 successful sends. At 2 slots the stats don't predict the wait, so a wait-aware gate would have to react to our own measured waits, and the client's `503` back-off already does that. **No measured benefit.** |
| **Drop it as a precondition** | waits are short and the stats don't predict them | Waits are 13 s median, 80 s worst. The stats predict them partly at 4 slots and not at all at 2. The gate costs hours to save seconds. **Yes.** |

**Recommendation (since adopted: waiting is now opt-in with `gtu start --wait-for-capacity`): drop the "wait for a free slot" gate as the thing that decides when work starts.** Send right away and rely on what already handles real overload: the client's `503`/timeout back-off, and pausing after repeated busy answers. Keep the live stats and `spare_capacity_advice` as information (it now reports our measured waits; see [why-live-stats-dont-predict-wait.md](why-live-stats-dont-predict-wait.md)) ("now is a busy hour, this will be slower"), not as a blocker.

The one thing that could reverse this: if a long job turns out to be throttled after sustained use, a reactive pause based on our *own* measured waits would be the right fix. That's cheap to add if a real overnight job ever shows it.

## Reproducing

```powershell
python scripts\gate_vs_send.py <output-folder>              # one paired sample
python scripts\gate_vs_send.py <output-folder> --burst 5    # one burst
python scripts\livestats_log.py <output-folder>             # one live-stats reading
python scripts\analyse_gate_vs_send.py research\data\gate-vs-send.csv research\data\qwen-api-livestats.csv
```

Schedule the first every 20 minutes and the third every minute.
