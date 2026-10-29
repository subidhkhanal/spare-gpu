# BharatCode live stats endpoints

- **Date:** 2026-10-30 (IST). Measurements were taken 2026-10-29 19:57–20:09 UTC, which is **01:27–01:39 IST**.
- **Endpoints:** `https://bharatcode.ai/api/serving/usage` and `https://bharatcode.ai/api/serving/history`
- **Method:** plain `curl` with no API key, at most one request every few seconds while probing parameters, and one per minute while polling. 25 requests in total.

## Summary

| Question | Answer |
|---|---|
| Needs auth? | **No.** Both return 200 with no `Authorization` header. |
| Queue depth and utilisation per model? | **Yes**, in `/usage`: `active_requests`, `queued_requests`, `slot_capacity`, `capacity_used_pct` and `available`, per model. |
| Lane breakdown? | **Yes.** Each model has an `api` lane (full fields) and a `chat` lane (active/queued only). |
| How fresh? | **Live.** `generated_at` equals the request time (under 1 s), and `Cache-Control: no-store`. History is sampled **once a minute** (5 samples per 5-minute bucket). |
| Rate-limit headers? | **None.** No `RateLimit-*`, `X-RateLimit-*` or `Retry-After`. The ETag changes every call because `generated_at` changes, so conditional requests save nothing. |
| Stable enough to depend on? | **Use it, but don't trust it blindly.** It is undocumented and unversioned, it silently ignores bad parameters, and the per-lane history only started today. Details below. |

## `/api/serving/usage`

Example response (19:57:49 UTC, unauthenticated):

```json
{
  "generated_at": "2026-10-29T19:57:49.449Z",
  "active_requests": 5,
  "queued_requests": 33,
  "slot_capacity": 7,
  "capacity_used_pct": 71,
  "available": true,
  "models": [
    {
      "id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash",
      "active_requests": 1, "queued_requests": 20, "slot_capacity": 3, "capacity_used_pct": 33, "available": true,
      "api":  { "active_requests": 1, "queued_requests": 20, "slot_capacity": 2, "capacity_used_pct": 50, "available": true },
      "chat": { "active_requests": 0, "queued_requests": 0 }
    },
    {
      "id": "qwen-3.8-27b", "name": "Qwen 3.8 27B",
      "active_requests": 4, "queued_requests": 13, "slot_capacity": 4, "capacity_used_pct": 100, "available": true,
      "api":  { "active_requests": 4, "queued_requests": 13, "slot_capacity": 4, "capacity_used_pct": 100, "available": true },
      "chat": { "active_requests": 0, "queued_requests": 0 }
    }
  ]
}
```

Response headers: `Server: nginx/1.27.5`, `X-Powered-By: Express`, `Cache-Control: no-store`, a weak `ETag`, `Access-Control-Expose-Headers: x-request-id`. No `x-request-id` was sent on this endpoint. Response time was 0.24–0.57 s.

Field notes:
- Top-level totals are the sum across models (slot capacity 7 = 4 + 3).
- `deepseek-v4.1-flash` has 3 slots in total but only 2 in the `api` lane. That implies 1 slot reserved for chat, but the `chat` object has no `slot_capacity` field, so this is our inference.
- `available: true` was reported even at 100% capacity with 13 queued, so it means "the model is up", **not** "there is free capacity".

## Polling: 10 samples, one per minute

Columns: `act/q` = active / queued requests, `slots` = slot capacity, `%` = capacity used. The two right-hand groups are the `api` lane of each model.

| UTC | resp | all: act/q (slots, %) | qwen api: act/q (slots, %) | deepseek api: act/q (%) |
|---|---|---|---|---|
| 19:59:43 | 0.57 s | 4 / 33 (7, 57%) | 4 / 13 (4, 100%) | 0 / 20 (0%) |
| 20:00:44 | 0.37 s | 4 / 32 (7, 57%) | 4 / 13 (4, 100%) | 0 / 19 (0%) |
| 20:01:45 | 0.26 s | 5 / 31 (7, 71%) | 4 / 12 (4, 100%) | 0 / 19 (0%) |
| 20:02:46 | 0.27 s | 4 / 32 (7, 57%) | 4 / 13 (4, 100%) | 0 / 19 (0%) |
| 20:03:47 | 0.24 s | 7 / 28 (7, 100%) | 4 / 12 (4, 100%) | 2 / 16 (100%) |
| 20:04:49 | 0.31 s | 7 / 25 (7, 100%) | 4 / 9 (4, 100%) | 2 / 16 (100%) |
| 20:05:50 | 0.48 s | 7 / 22 (7, 100%) | 4 / 6 (4, 100%) | 2 / 16 (100%) |
| 20:06:51 | 0.29 s | 6 / 23 (7, 86%) | 4 / 6 (4, 100%) | 1 / 17 (50%) |
| 20:07:52 | 0.28 s | 4 / 24 (7, 57%) | 4 / 6 (4, 100%) | 0 / 18 (0%) |
| 20:08:54 | 0.34 s | 4 / 22 (7, 57%) | 4 / 5 (4, 100%) | 0 / 17 (0%) |

What this shows:
- **The numbers move minute to minute.** The qwen API queue drained from 13 to 5 in 10 minutes, about 0.8 requests a minute, so the endpoint is genuinely live.
- **Qwen was saturated the whole time, at about 01:30 IST.** All 4 API slots were busy and 5–13 requests were queued. The project's assumption that it's "quiet overnight in India" was **false tonight**. A fixed 00:00–09:00 IST window would have sent work straight into a full queue.
- **The DeepSeek API lane showed 16–20 queued with 0 active** for most samples. A queue with idle slots means `queued_requests` is not simply "waiting for a free slot". It could be stuck, rate-limited or account-capped requests. We don't know which. So `queued_requests` alone can't be read as "busy", and `active < slot_capacity` alone can't be read as "free".

## `/api/serving/history`

Example: `?hours=6&bucket=5m&model=qwen-3.8-27b&lane=api`

```json
{
  "status": "ok", "model": "qwen-3.8-27b", "lane": "api",
  "window_hours": 6, "bucket_minutes": 5,
  "buckets": [
    { "started_at": "2026-10-29T16:25:00.000Z", "avg_capacity_utilization_pct": 0, "avg_queue_depth": 3,
      "avg_running_requests": 0, "sample_count": 1 },
    "...",
    { "started_at": "2026-10-29T19:55:00.000Z", "avg_capacity_utilization_pct": 100, "avg_queue_depth": 13.33,
      "avg_running_requests": 4, "sample_count": 3 }
  ]
}
```

Bucket fields are `started_at`, `avg_capacity_utilization_pct`, `avg_queue_depth`, `avg_running_requests` and `sample_count`. Note these names differ from `/usage`. Results of probing the parameters:

| Parameter | What happened |
|---|---|
| `bucket` | `1m`, `5m`, `15m`, `1h` (reported as `60`) accepted. **Unknown values like `7m` silently fall back to 5m.** |
| `hours` | 1, 6, 24, 168 accepted. **`hours=abc` silently falls back to 24.** Default is 24. |
| `lane` | Only `api` is honoured. `chat`, `bogus` or no lane all return `"lane": "combined"`, so no chat-only history is exposed. |
| `model` | Valid id filters to that model. **An unknown model (`nope`) silently returns the all-models series**, and the response then has no `model`/`lane` keys. |
| Retention | Combined data goes back **7 days** (`hours=168&bucket=1h` gave 168 buckets from 2026-10-22). **Per-lane (`api`) data only starts at 2026-10-29 ~16:25 UTC**, so lane tracking appears to have been switched on the same day. |

All calls returned 200. Bad input never produced a 4xx.

## Is it safe to depend on?

Signs it's an **internal endpoint that could change without notice**:
- It isn't in the public API docs (`/build`), which only describe `/api/model/v1`.
- It sits under `/api/serving/`, not the versioned `/api/model/v1/`.
- The two endpoints use different field names for the same idea (`capacity_used_pct` vs `avg_capacity_utilization_pct`).
- It accepts any input silently. A typo in a parameter returns different data instead of an error.
- Per-lane history started on the day we looked, so it's being actively changed.
- There's no rate-limit guidance at all.

Signs it's **usable**:
- It needs no auth, responds quickly (under 0.6 s) and gives live data.
- It powers the public Live Stats page, so it's unlikely to vanish quietly. Its shape could still change, though.

**Verdict:** use `/usage` as the primary signal, but treat it as untrusted input. Validate every response strictly, fail over to our own probes on anything unexpected, and poll at most once a minute, the same rate it's sampled at. Polling faster gives no fresher data. Don't depend on `/history` for decisions. It's fine for the morning report's context ("what was the queue like overnight").

## Phase 2 plan update: capacity gate

The gate decides whether it's quiet enough to send the next job step. It replaces the original "three equal signals" design with **live stats first, our own probes as fallback**.

### Primary signal: `/api/serving/usage`
- Poll at most **once per 60 s**, and only while there's work queued. No polling when idle.
- Validate with a strict Pydantic model. The reading is **unusable** if any of these happen:
  - a network error, a timeout over 5 s, or a non-200 status
  - the body isn't JSON or a field is missing or the wrong type
  - the target model is missing, or the model has no `api` lane
  - `generated_at` is more than 3 minutes off our clock
- A reading counts as **quiet** for our model's `api` lane only when **all** of these hold:
  - `available` is true
  - `active_requests < slot_capacity`, so a slot is free
  - `queued_requests == 0`, so we'd be served now and not queue behind others
- Both conditions are needed because of the DeepSeek observation, where queued > 0 while slots sat idle.
- Thresholds are configurable (e.g. tolerate `queued_requests <= 1`). The defaults are strict, matching "only spare capacity".

### Fallback signal: our own probes
Used only while live stats are unusable.
- **Passive first:** every real request already logs its response time and status. The last 5 requests give a 503 rate and a median latency.
- **Active probe** only if there's no recent traffic: a ~16-token `/chat/completions` request, at most once every 5 minutes, logged like any other request.
- Busy if 2 or more of the last 5 got a 503/429, or median latency is more than 3× a rolling baseline.
- In fallback mode, the configurable time window (default 00:00–09:00 IST) **also has to be open**, as an extra safety margin. When live stats are working, the window is **not** used by default, because tonight showed night isn't reliably quiet. It stays available as an opt-in setting.

### Hysteresis
- Closed → open: **3 consecutive quiet readings**, about 3 minutes.
- Open → closed: **2 consecutive busy readings**, or **immediately** on any 503/429 from our own request.
- Switching between live stats and fallback is logged, and doesn't by itself open the gate.

### Recording, for `gtu why` and the Phase 4 report
- Each request-log row gains `gate_source` (`livestats` or `probe`) plus `queue_at_send` and `active_at_send` from the last reading. That's what makes "share of requests sent while BharatCode's queue was empty" possible later.
- A new `gate_readings` table stores each poll: timestamp, source, model, active, queued, slots, decision and reason.
- Example `gtu why` output: `Paused (live stats): qwen-3.8-27b api lane 4/4 slots busy, 6 queued. Need 3 quiet readings in a row; have 0. Next check in 60 s.`

### Tests (fake only)
- Extend `fakeapi.FakeBharatCode` with a scriptable `/api/serving/usage` handler. It should support quiet, busy, queued-but-idle, malformed JSON, missing model, stale `generated_at`, 500 and timeout responses.
- Tests to write:
  - the gate opens only after 3 quiet readings
  - it closes on 2 busy readings or one 503
  - it falls back to probes on each kind of bad reading and returns to live stats when they recover
  - the time window is enforced only in fallback
  - polling never goes faster than once a minute
- Resumability tests are unchanged from the original Phase 2 plan.

### Guesses to flag
- That `queued_requests` in the `api` lane is the queue our requests would join.
- That one free slot means our request starts promptly.
- That the endpoint tolerates once-a-minute polling. No guidance exists; that's the same rate the Live Stats page samples at.
