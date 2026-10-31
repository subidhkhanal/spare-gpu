"""Which predicts the next request's queue wait best: BharatCode's live stats, or our own recent waits?

Usage: python validate_wait_predictor.py <gate-vs-send.csv> [N] [max-age-minutes]

Standard library only. Every send in the experiment (paired and burst), in time order, is the
history; each successful paired send is a target. Predictors, all known before the send:
- queued:   this lane's queued_requests at send time
- used_pct: this lane's active / slots at send time, as a percentage
- ours:     median measured wait of our previous N successful requests no older than max-age
Reported: correlation with the actual wait, and the mean absolute error of a prediction in
seconds. The stats aren't in seconds, so each is mapped through a straight line fitted with that
target left out (leave-one-out); ours is used as is, and also through a fitted line, for fairness.
Baseline: the median of all other targets' waits (knows nothing about the moment).
"""

import csv
import statistics
import sys
from datetime import datetime, timedelta


def pearson(a, b):
    if len(a) < 3:
        return float("nan")
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


def loo_line_mae(x, y):
    """Mean absolute error of y predicted from x by a line fitted without that point."""
    errors = []
    for i in range(len(x)):
        xs, ys = x[:i] + x[i + 1:], y[:i] + y[i + 1:]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        vx = sum((v - mx) ** 2 for v in xs)
        slope = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / vx if vx else 0.0
        errors.append(abs(my + slope * (x[i] - mx) - y[i]))
    return statistics.mean(errors)


def main(path, n=3, max_age=60.0):
    rows = sorted(csv.DictReader(open(path, encoding="utf-8")), key=lambda r: r["sent_at"] or r["sample_utc"])
    history, targets = [], []
    for r in rows:
        t = datetime.fromisoformat(r["sent_at"] or r["sample_utc"])
        ok = r["status"] == "200" and r["queue_wait_ms"] != ""
        wait = float(r["queue_wait_ms"]) / 1000 if ok else None
        if r["kind"] == "paired" and ok and r["slots"]:
            recent = [w for (ht, w) in history if t - ht <= timedelta(minutes=max_age)][-n:]
            targets.append(dict(
                slots=r["slots"], wait=wait, queued=float(r["queued"]),
                used_pct=100 * float(r["active"]) / float(r["slots"]),
                ours=statistics.median(recent) if len(recent) == n else None,
            ))
        if ok:
            history.append((t, wait))

    print(f"N={n} previous requests, no older than {max_age:.0f} min\n")
    for label, group in (("all", targets), ("4 slots", [t for t in targets if t["slots"] == "4"]),
                         ("2 slots", [t for t in targets if t["slots"] == "2"])):
        covered = [t for t in group if t["ours"] is not None]
        print(f"{label}: {len(group)} targets, {len(covered)} with {n} recent waits of our own")
        for name, pool in (("all targets", group), (f"targets with {n} recent waits", covered)):
            if len(pool) < 4:
                print(f"  {name}: too few to compare")
                continue
            y = [t["wait"] for t in pool]
            base = statistics.mean(abs(statistics.median(y[:i] + y[i + 1:]) - y[i]) for i in range(len(y)))
            print(f"  {name} (n={len(pool)}): baseline error {base:5.1f} s")
            for key in ("queued", "used_pct", "ours"):
                if key == "ours" and pool is not covered:
                    continue
                x = [t[key] for t in pool]
                r, rho = pearson(x, y), pearson(ranks(x), ranks(y))
                line = loo_line_mae(x, y)
                extra = f"  as is {statistics.mean(abs(a - b) for a, b in zip(x, y)):5.1f} s" if key == "ours" else ""
                print(f"    {key:8s} r {r:+.2f}  rho {rho:+.2f}  error (fitted line) {line:5.1f} s{extra}")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], *(int(sys.argv[2]),) if len(sys.argv) > 2 else (), *(float(sys.argv[3]),) if len(sys.argv) > 3 else ())
