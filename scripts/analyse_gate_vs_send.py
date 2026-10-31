"""Reproduce the numbers in research/gate-vs-send-experiment.md from the raw CSVs.

Usage: python analyse_gate_vs_send.py <gate-vs-send.csv> <qwen-api-livestats.csv>

Standard library only. The gate replay uses spare-gpu's shipped rule (gate.py): a reading is
quiet when the lane is up, a slot is free and no more than 5 are queued; the gate opens after
3 quiet readings in a row and closes after 2 busy ones. A gap of over 3 minutes in the log
(laptop asleep) resets it, and a wait cut off by a gap is reported as "at least".
"""

import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MAX_QUEUED, QUIET_NEEDED, BUSY_NEEDED, GAP_SECONDS = 5, 3, 2, 180


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(ms):
    s = [x / 1000 for x in ms]
    return f"n={len(s):2d}  median {pct(s, .5):5.1f} s  p90 {pct(s, .9):5.1f} s  max {max(s):5.1f} s" if s else "n= 0"


def pearson(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def ranks(a):
    order = sorted(range(len(a)), key=lambda i: a[i])
    out, i = [0.0] * len(a), 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2
        i = j + 1
    return out


def load_live(path):
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        t = datetime.fromisoformat(r["fetched_utc"])
        ok = r["http_status"] == "200" and r["slot_capacity"]
        rows.append((t, dict(available=r["available"] == "True", active=int(r["active_requests"]),
                             queued=int(r["queued_requests"]), slots=int(r["slot_capacity"])) if ok else None))
    return rows


def replay(live):
    """[(time, 'open'|'closed')] with None entries marking gaps."""
    out, quiet, busy, is_open, prev = [], 0, 0, False, None
    for t, s in live:
        if prev and (t - prev).total_seconds() > GAP_SECONDS:
            out.append(None)
            quiet = busy = 0
            is_open = False
        good = s and s["available"] and s["active"] < s["slots"] and s["queued"] <= MAX_QUEUED
        quiet, busy = (quiet + 1, 0) if good else (0, busy + 1)
        is_open = busy < BUSY_NEEDED if is_open else quiet >= QUIET_NEEDED
        out.append((t, "open" if is_open else "closed"))
        prev = t
    return out


def delays(states):
    """{time: (minutes until the gate is open, cut off by a gap?)}"""
    res, next_open, seg_end = {}, None, None
    for item in reversed(states):
        if item is None:
            next_open = seg_end = None
            continue
        t, g = item
        seg_end = seg_end or t
        if g == "open":
            next_open = t
        res[t] = ((next_open - t).total_seconds() / 60, False) if next_open else ((seg_end - t).total_seconds() / 60, True)
    return res


def main(sends_path, live_path):
    live = load_live(live_path)
    print(f"Live stats: {len(live)} readings, {live[0][0].astimezone(IST):%d %b %H:%M} to {live[-1][0].astimezone(IST):%d %b %H:%M} IST, "
          f"{sum(1 for _, s in live if s is None)} failed fetches")

    print("\nGaps over 3 minutes:")
    for (a, _), (b, _) in zip(live, live[1:]):
        if (b - a).total_seconds() > GAP_SECONDS:
            print(f"  {a.astimezone(IST):%d %b %H:%M} to {b.astimezone(IST):%d %b %H:%M}  ({(b - a).total_seconds() / 60:.0f} min)")

    print("\nSlot capacity changes:")
    last = None
    for t, s in live:
        if s and s["slots"] != last:
            print(f"  {t.astimezone(IST):%d %b %H:%M}  {s['slots']} slots")
            last = s["slots"]

    full = [s for _, s in live if s]
    print(f"\nMinutes with every slot busy: {sum(1 for s in full if s['active'] >= s['slots'])} of {len(full)}")

    states = replay(live)
    real = [x for x in states if x]
    print(f"Gate open (replayed): {sum(1 for _, g in real if g == 'open')} of {len(real)} minutes")

    print("\nLongest stretches the gate stays closed:")
    stretches, start, prev_t = [], None, None
    for item in states + [None]:
        if item is None:
            if start:
                stretches.append((start, prev_t, True))
            start = None
            continue
        t, g = item
        if g == "closed" and start is None:
            start = t
        elif g == "open" and start:
            stretches.append((start, t, False))
            start = None
        prev_t = t
    for a, b, cut in sorted(stretches, key=lambda x: x[0] - x[1])[:8]:
        print(f"  {a.astimezone(IST):%d %b %H:%M} to {b.astimezone(IST):%d %b %H:%M}  "
              f"{'>=' if cut else '  '}{(b - a).total_seconds() / 60:4.0f} min")

    wait_for = delays(states)
    print("\nIf a job arrived at a random minute, minutes until the gate opens (>= cut off by a gap):")
    by_hour = defaultdict(list)
    for t, (d, cut) in wait_for.items():
        lt = t.astimezone(IST)
        by_hour[(lt.date(), lt.hour)].append((d, cut))
    for (day, hour), v in sorted(by_hour.items()):
        ds = [d for d, _ in v]
        mark = ">=" if any(c for _, c in v) else "  "
        now = 100 * sum(1 for d, c in v if d == 0 and not c) / len(v)
        print(f"  {day:%d %b} {hour:02d}:00  open now {now:3.0f}%  median {mark}{pct(ds, .5):4.0f}  max {mark}{max(ds):4.0f}")

    sends = list(csv.DictReader(open(sends_path, encoding="utf-8")))
    for r in sends:
        r["t"] = datetime.fromisoformat(r["sample_utc"])
        r["ok"] = r["status"] == "200" and r["queue_wait_ms"] != ""
    paired = [r for r in sends if r["kind"] == "paired"]
    ok = [r for r in paired if r["ok"]]
    print(f"\nPaired sends: {len(paired)}, succeeded {len(ok)}")
    print("  all          ", summary([float(r["queue_wait_ms"]) for r in ok]))
    for v in ("quiet", "busy"):
        sub = [r for r in paired if r["verdict_default"] == v]
        print(f"  gate {v:5s}   ", summary([float(r["queue_wait_ms"]) for r in sub if r["ok"]]), f" failures {sum(1 for r in sub if not r['ok'])}")
    for s in ("4", "2"):
        sub = [r for r in ok if r["slots"] == s]
        hours = sorted({r["t"].astimezone(IST).hour for r in sub})
        print(f"  {s} slots      ", summary([float(r["queue_wait_ms"]) for r in sub]), f" hours {hours}")
    print("  by hour (IST):")
    for h in sorted({r["t"].astimezone(IST).hour for r in paired}):
        sub = [r for r in paired if r["t"].astimezone(IST).hour == h]
        slots = "/".join(sorted({r["slots"] for r in sub if r["slots"]}))
        print(f"    {h:02d}  {slots:3s} slots  ", summary([float(r["queue_wait_ms"]) for r in sub if r["ok"]]),
              f" failures {sum(1 for r in sub if not r['ok'])}")

    print("\nCorrelation with the queue wait (Pearson / Spearman):")
    w = [float(r["queue_wait_ms"]) for r in ok]
    for col in ("active", "queued", "slots", "total_active", "total_queued"):
        x = [float(r[col]) for r in ok]
        print(f"  {col:13s} {pearson(x, w):+.2f} / {pearson(ranks(x), ranks(w)):+.2f}")
    for s in ("4", "2"):
        sub = [r for r in ok if r["slots"] == s]
        x, y = [float(r["queued"]) for r in sub], [float(r["queue_wait_ms"]) for r in sub]
        print(f"  queued, {s} slots only (n={len(sub)}) {pearson(x, y):+.2f} / {pearson(ranks(x), ranks(y)):+.2f}")

    print("\nEach paired sample: queue wait if sent now vs replayed wait for the gate:")
    times = sorted(wait_for)
    gate_total = send_total = 0.0
    failures = 0
    for r in paired:
        near = min(times, key=lambda t: abs((t - r["t"]).total_seconds()))
        if abs((near - r["t"]).total_seconds()) > 90 or not r["slots"]:
            continue
        d, cut = wait_for[near]
        gate_total += d
        if r["ok"]:
            send_total += float(r["queue_wait_ms"]) / 1000
        else:
            failures += 1
        sent = f"{float(r['queue_wait_ms']) / 1000:5.1f} s" if r["ok"] else " failed"
        print(f"  {r['t'].astimezone(IST):%d %b %H:%M}  {r['active']}/{r['slots']} busy, {r['queued']} queued  sent now {sent}  gate {'>=' if cut else '  '}{d:4.0f} min")
    print(f"  total: sent now {send_total / 60:.1f} min of waiting and {failures} failures; gate at least {gate_total:.0f} min")

    print("\nBursts:")
    for r in sends:
        if r["kind"] == "burst":
            wait = f"{float(r['queue_wait_ms']) / 1000:5.1f} s" if r["queue_wait_ms"] else "   n/a"
            print(f"  {r['t'].astimezone(IST):%d %b %H:%M:%S} #{r['burst_index']}  {r['active']}/{r['slots']} busy, {r['queued']} queued  "
                  f"status {r['status'] or '-':3s}  wait {wait}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
