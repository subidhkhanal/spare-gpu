# Does waiting for a free slot beat just sending?

- **Dates:** 2026-10-30 03:07 IST to 2026-10-31 02:50 IST
- **Model and lane:** `qwen-3.8-27b`, `api` lane
- **Script:** [scripts/gate_vs_send.py](../scripts/gate_vs_send.py), run by Windows Task Scheduler every 20 minutes
- **Raw data:**
  - [data/gate-vs-send.csv](data/gate-vs-send.csv): one row per send
  - [data/qwen-api-livestats-2026-10-30.csv](data/qwen-api-livestats-2026-10-30.csv): BharatCode's live stats, logged once a minute
- **Cost:** 50 requests of about 21 tokens each, about 1,000 tokens in all

## Question

spare-gpu's gate holds work back until BharatCode's live stats show a free slot. That only helps if what the live stats say predicts what a request actually meets.

## Method

Every 20 minutes the script:

1. read `/api/serving/usage` and recorded what the gate would decide, under two rules:
   - **default:** a queue of up to 5 is tolerated;
   - **strict:** any queue at all closes the gate;
2. then sent one tiny chat request **regardless of the decision**;
3. recorded the queue wait BharatCode reports for that request (`x-bharatcode-queue-wait-ms`), plus the status and any error.

There were two kinds of sample:

- **Paired:** one send per sample, 40 in all.
- **Burst:** 5 sends in a row, about 10 seconds apart. There were two bursts, at 03:07 and 22:46 on 30 Oct.

Sends were never retried, so a busy reply counts as a failure.

## Results

### The gate's verdict predicts the wait almost perfectly

| Gate said | Sends | Succeeded | Queue wait: median | mean | max | waits over 5 s |
|---|---|---|---|---|---|---|
| **quiet** | 6 | 6 | **0.0 s** | 0.0 s | 0.0 s | 0 |
| **busy** | 43 | 38 | **14.5 s** | 26.0 s | 80.1 s | 33 of 38 |
| (live stats unreachable) | 1 | 0 | n/a | n/a | n/a | n/a |

- **Every send the gate called quiet** was admitted at once: a wait of 0 ms in all six cases.
- **Every failed send happened when the gate said busy.** That's four `503 busy` replies and one 180-second timeout.
- Of the 38 busy sends that succeeded, 5 got in within 5 seconds and one got in within 1 second.
- The **default and strict rules gave the same verdict on every sample**, so the queue tolerance of 5 made no difference in this data.

### Which number matters

Here is the correlation between the queue wait and each live-stats figure at send time (paired sends):

| Live-stats figure | Correlation with the wait |
|---|---|
| this lane's `queued_requests` | **0.47** |
| this lane's `active_requests` | -0.18 |
| all lanes' total queued | 0.00 |
| all lanes' total active | -0.05 |

The figure that matters is **this model's own lane**; the site-wide totals tell you nothing. One case shows why the slot count matters as well as the queue: at 01:50 on 31 Oct, a send with 0 queued but 4 of 4 slots busy still waited 17 s.

### Capacity changes on a daily schedule

The Qwen API lane's slot count, from the minute-by-minute log:

| From (IST) | Slots |
|---|---|
| 30 Oct 01:47 (log start) | 4 |
| 30 Oct 05:30 | **2** |
| 30 Oct 13:30 | 4 |
| 31 Oct 05:30 | **2** |

This is a schedule, not a one-off change: **2 slots from 05:30 to 13:30 IST (00:00 to 08:00 UTC), 4 otherwise.** Earlier notes treated the 30 Oct drop as a single event. The morning waits of 31–80 s happened at half capacity.

### Time of day

Share of minutes in which the lane had a queue, by hour (IST):

| Hour | 00 | 01 | 02 | 03 | 04 | 05 | 07 | 08–11 | 12 | 13 | 22 | 23 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Minutes with a queue | **3%** | 40% | 68% | 50% | 48% | 46% | 88% | **100%** | 98% | 85% | 100% | 100% |
| Mean queued | 0.1 | 2.2 | 2.1 | 1.0 | 0.8 | 1.2 | 3.4 | 4.7–7.0 | 2.7 | 3.1 | 6.0 | 6.2 |

- Midnight to 01:00 IST was almost always free.
- 08:00 to 12:00 was always queued. That's during the 2-slot window.
- The late evening (22–23) was full even at 4 slots.

Free stretches and queued stretches alternate. The lane queued 15 separate times, and the longest stretch with a queue lasted 8 hours 11 minutes, from 04:42 to 12:53 on 30 Oct, covering most of the 2-slot window.

### Bursts

Back-to-back sends add to the queue they're measuring:

| Burst | Gate said | Queued at the time | Waits | Failures |
|---|---|---|---|---|
| 03:07, 30 Oct | busy | 2–4 | 3–12 s | none |
| 22:46, 30 Oct | busy | 5–6 | 12–72 s | one `503` |

## Limits

- **Only 6 sends got a quiet verdict**, and all of them were between 00:00 and 02:50 on 31 Oct. The result "quiet means no wait" is consistent, but it rests on a small sample from one part of the night.
- **Coverage has gaps.** The machine was off or asleep from 13:38 to 22:46 on 30 Oct and 06:09 to 07:46 on 30 Oct, so the afternoon is missing. There are 40 paired samples of the 72 planned.
- **Only one model and lane were measured:** `qwen-3.8-27b` on the API lane. The DeepSeek lane was logged but not tested with sends.
- The wait figure is BharatCode's own header. It's undocumented, so it could change meaning without notice.
- These are single tiny requests. A real job's steps are large, but the admission wait should depend on slots, not on size; that's an assumption, not something measured here.

## Recommendation

1. **Keep the gate.** It costs nothing in the case it says "go", and avoids a median 14.5 s wait (up to 80 s) plus every failure seen, in the case it says "wait". For unattended background work, waiting is free; failures and long queues aren't, and sending into a queue also slows everyone else on the lane.
2. **Decide on this model's lane only**: its active, queued and slot figures. Ignore the site-wide totals, which carried no signal here.
3. **Keep the queue tolerance, but don't rely on it.** It never changed a verdict in this data. It stays because BharatCode's pacing can show a queue while slots sit idle (seen in earlier tests), but there's no evidence either way from this run.
4. **Plan for the daily slot schedule.** Expect 2 slots from 05:30 to 13:30 IST. The best window seen was **00:00–01:00 IST**, which `spare_capacity_advice` / `gtu advice` already surfaces from BharatCode's own history.
5. **Don't send in bursts.** Five back-to-back sends into a busy lane waited up to 72 s and hit a 503; space requests out and check the gate again between them.

## Reproducing

```powershell
python scripts\gate_vs_send.py <output-folder>              # one paired sample
python scripts\gate_vs_send.py <output-folder> --burst 5    # one burst
```

Schedule the first every 20 minutes. Run `scripts\livestats_log.py` every minute alongside it for the slot and queue history.
