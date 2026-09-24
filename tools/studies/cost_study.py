#!/usr/bin/env python3
"""Where the ~0.2% per trade goes: entry cost, exit cost, and the move between.

Per fill, against SIP 1m bars (runway cache):
  entry_cost = fill / close(bar before the entry minute) - 1      [paid over tape]
  move       = close(exit minute) / close(bar before entry) - 1   [the market]
  exit_cost  = close(exit minute) / exit fill - 1                 [given up to tape]
  realized   = exit fill / fill - 1  ==  move - entry_cost - exit_cost (approx.)
All in %. Positive cost = money lost to execution. Also: exit fill vs the
shelf (stop) it was selling, and vs the exit minute's LOW (a fill under the
bar's low is a print the tape never showed at that size).
"""
from __future__ import annotations

import collections
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402


def main():
    cache = rs._load_cache()
    rows = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:
            continue
        sym, et, xt = str(r.get("symbol") or ""), r.get("entry_time"), r.get("exit_time")
        px, xp = r.get("entry_price"), r.get("exit_price")
        if (not rs.SYM_RE.match(sym) or not isinstance(et, (int, float))
                or not isinstance(xt, (int, float)) or not px or not xp):
            continue
        B = cache.get((sym, bars.day_of(et)))
        if not B:
            continue
        t, _o, h, l, c, _v = B
        i = bars.index_at(t, et)
        j = bars.index_at(t, xt)
        if i < 1 or j < 0 or j < i:
            continue
        ref = c[i - 1]
        f = r.get("features") or {}
        sp = f.get("spread_r")
        e, s = float(px), r.get("stop_price")
        spread_pct = (sp * (e - s) / e * 100) if isinstance(sp, (int, float)) and s and e > s else None
        shelf = r.get("exit_shelf_price")
        rows.append({
            "sym": sym, "day": bars.day_of(et), "px": e,
            "entry_cost": (e / ref - 1) * 100,
            "move": (c[j] / ref - 1) * 100,
            "exit_cost": (c[j] / float(xp) - 1) * 100,
            "realized": (float(xp) / e - 1) * 100,
            "under_shelf": ((float(shelf) - float(xp)) / float(shelf) * 100) if shelf else None,
            "under_low": float(xp) < l[j] - 1e-9,
            "reason": str(r.get("close_reason")),
            "hour": bars.et_minutes(et) // 60,
            "spread_pct": spread_pct,
            "hold": r.get("hold_sec"),
        })
    print(f"fills scored: {len(rows)}\n")

    def line(label, xs):
        if len(xs) < 10:
            return
        m = lambda k: rs.mean([x[k] for x in xs if x.get(k) is not None])
        us = [x["under_shelf"] for x in xs if x.get("under_shelf") is not None]
        print(f"  {label:<22}{len(xs):>5}{m('realized'):>+9.3f}{m('move'):>+9.3f}"
              f"{m('entry_cost'):>+9.3f}{m('exit_cost'):>+9.3f}"
              f"{m('entry_cost') + m('exit_cost'):>+9.3f}"
              f"{(rs.mean(us) if us else float('nan')):>+9.3f}"
              f"{sum(x['under_low'] for x in xs) / len(xs):>8.0%}")

    hdr = (f"  {'group':<22}{'n':>5}{'realized':>9}{'market':>9}{'entry $':>9}"
           f"{'exit $':>9}{'both $':>9}{'<shelf':>9}{'<low':>8}")
    print("== DECOMPOSITION (%, per trade) ==")
    print(hdr)
    line("ALL", rows)
    print("\n== BY PRICE ==")
    print(hdr)
    for lo, hi in ((0, 10), (10, 20), (20, 50), (50, 1e9)):
        line(f"${lo}-{hi if hi < 1e9 else '+'}", [x for x in rows if lo <= x["px"] < hi])
    print("\n== BY LOGGED SPREAD AT ARM (IEX, overstates) ==")
    print(hdr)
    sp = sorted(x["spread_pct"] for x in rows if x["spread_pct"] is not None)
    if sp:
        a, b = rs.q(sp, 1 / 3), rs.q(sp, 2 / 3)
        line(f"tight <= {a:.2f}%", [x for x in rows if x["spread_pct"] is not None and x["spread_pct"] <= a])
        line(f"mid", [x for x in rows if x["spread_pct"] is not None and a < x["spread_pct"] < b])
        line(f"wide >= {b:.2f}%", [x for x in rows if x["spread_pct"] is not None and x["spread_pct"] >= b])
        line("no spread logged", [x for x in rows if x["spread_pct"] is None])
    print("\n== BY HOUR (ET) ==")
    print(hdr)
    for hr in range(9, 16):
        line(f"{hr}:00", [x for x in rows if x["hour"] == hr])
    print("\n== BY EXIT REASON ==")
    print(hdr)
    g = collections.defaultdict(list)
    for x in rows:
        g[x["reason"]].append(x)
    for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))[:6]:
        line(k, v)
    print("\n== BY HOLD ==")
    print(hdr)
    for lo, hi, lab in ((0, 60, "< 1 min"), (60, 300, "1-5 min"), (300, 1800, "5-30 min"), (1800, 1e9, "> 30 min")):
        line(lab, [x for x in rows if isinstance(x.get("hold"), (int, float)) and lo <= x["hold"] < hi])


if __name__ == "__main__":
    main()
