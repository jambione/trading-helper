#!/usr/bin/env python3
"""Score the operator's "Buy?" calls from the book (ai_reports/my_calls.jsonl).

Each call is scored the way the indicator studies scored the desk's squares, so the two compare directly:
  entry   open of the first SIP 1m bar starting after the click
  net15   close 15 min after entry (capped 15:55) / entry - 1 - 0.20% round trip; also net30 and the best/worst
          price inside the 30 min (MFE/MAE, before cost)
  lift    net15 minus the mean net15 of random minutes in the same name, day and clock hour
          (every 5th RTH minute 09:40-15:30)
Repeat clicks on one name within 5 minutes count once. Calls outside 09:30-15:45 are listed but not scored.

Run on the mini (needs Alpaca data): .venv/bin/python tools/studies/my_calls_score.py [--since 2026-10-05]
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics
import sys
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import indicator_levels_study as ils  # noqa: E402

COST = 0.0020


def at(day, hh, mm):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=bars.ET).timestamp()


def path_from(t, o, h, l, c, i, day, hold):
    """(entry, net, mfe, mae) entering at the open of bar i."""
    if i >= len(t):
        return None
    px = float(o[i])
    tx = min(float(t[i]) + hold * 60, at(day, 15, 55))
    j = int(np.searchsorted(t, tx, side="right")) - 1
    if j < i or px <= 0:
        return None
    return (px, float(c[j]) / px - 1 - COST, float(h[i:j + 1].max()) / px - 1, float(l[i:j + 1].min()) / px - 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2000-01-01")
    ap.add_argument("--file", default=os.path.join(ROOT, "ai_reports", "my_calls.jsonl"))
    a = ap.parse_args()
    calls = []
    for line in open(a.file):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        d = bars.day_of(float(r["ts"]))
        if d >= a.since:
            calls.append({**r, "day": d})
    calls.sort(key=lambda r: r["ts"])
    kept, last = [], {}
    for r in calls:
        if r["ts"] - last.get(r["symbol"], -1e9) >= 300:
            kept.append(r)
            last[r["symbol"]] = r["ts"]
    if not kept:
        print("no calls yet")
        return
    want = collections.defaultdict(set)
    for r in kept:
        want[r["day"]].add(r["symbol"])
    cache = ils.fetch_ext(want)

    rows, per_day_lift = [], collections.defaultdict(list)
    for r in kept:
        df = cache.get((r["symbol"], r["day"]))
        m = bars.et_minutes(r["ts"])
        tag = ""
        if df is None or len(df) < 30:
            tag = "no bars"
        elif not (9 * 60 + 30 <= m <= 15 * 60 + 45):
            tag = "outside RTH"
        if tag:
            rows.append((r, None, None, None, tag))
            continue
        t = np.array([x.timestamp() for x in df.index])
        o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        i = int(np.searchsorted(t, r["ts"], side="right"))            # first bar starting after the click
        p15, p30 = path_from(t, o, h, l, c, i, r["day"], 15), path_from(t, o, h, l, c, i, r["day"], 30)
        hour = m // 60
        ctl = []
        for k in range(len(t)):
            mk = bars.et_minutes(t[k])
            if 9 * 60 + 40 <= mk <= 15 * 60 + 30 and mk % 5 == 0 and mk // 60 == hour:
                p = path_from(t, o, h, l, c, k + 1, r["day"], 15)
                if p:
                    ctl.append(p[1])
        lift = (p15[1] - statistics.mean(ctl)) if p15 and ctl else None
        if lift is not None:
            per_day_lift[r["day"]].append(lift)
        rows.append((r, p15, p30, lift, ""))

    bp = lambda x: "—" if x is None else f"{x * 1e4:+.0f}"
    print(f"{'time':19} {'sym':6} {'shown':>8} {'entry':>8} {'net15':>6} {'net30':>6} {'MFE30':>6} {'MAE30':>6} {'lift':>6}")
    for r, p15, p30, lift, tag in rows:
        when = datetime.fromtimestamp(r["ts"], bars.ET).strftime("%m-%d %H:%M:%S")
        shown = f"{r['shown_price']:.2f}" if r.get("shown_price") else "—"
        if tag:
            print(f"{when:19} {r['symbol']:6} {shown:>8}  {tag}")
            continue
        print(f"{when:19} {r['symbol']:6} {shown:>8} {p15[0] if p15 else float('nan'):>8.2f} {bp(p15 and p15[1]):>6} "
              f"{bp(p30 and p30[1]):>6} {bp(p30 and p30[2]):>6} {bp(p30 and p30[3]):>6} {bp(lift):>6}")
    n15 = [p15[1] for _, p15, _, _, tag in rows if p15 and not tag]
    lifts = [x for v in per_day_lift.values() for x in v]
    if n15:
        print(f"\ncalls scored {len(n15)} (of {len(rows)}): net15 mean {statistics.mean(n15) * 1e4:+.1f} bp, "
              f"win {np.mean(np.array(n15) > 0):.0%}")
    if lifts:
        dm = [statistics.mean(v) for v in per_day_lift.values()]
        t = statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm))) if len(dm) > 2 and statistics.stdev(dm) > 0 else None
        print(f"lift vs random minutes (same name, day, hour): {statistics.mean(lifts) * 1e4:+.1f} bp over {len(lifts)} calls, "
              f"{len(dm)} days" + (f", day-clustered t {t:+.1f}" if t is not None else ""))
        print("For reference: desk squares ran about -21 to -27 bp vs the same baseline (INDICATOR_LEVELS_2026-10-02).")


if __name__ == "__main__":
    main()
