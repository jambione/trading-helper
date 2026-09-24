#!/usr/bin/env python3
"""Exit work: (1) regret by exit reason, (2) replay of the live exit vs others.

Fills from ai_reports/outcomes.jsonl, SIP 1m RTH bars from the runway cache.

1. REGRET  after each real exit, from the exit price: return at 5/15/30m, best
           move in 30m, and whether +1% came before -1%. Grouped by
           close_reason. High "ran after" = that exit sells runway away.

2. REPLAY  every shape starts at the fill, bars after the entry minute, 60m
           time stop at the bar close. Conservative bar order: bar k's low is
           tested against the stop set by bars < k (gap-through exits at the
           open), then bar k raises the stop. Net subtracts a 0.20% round trip.
           LIVE_NOW approximates today's config at 1m resolution: stop ~1%
           under the fill; if the first full bar never trades above the fill,
           sell at its close (60s rule); arm when a high reaches +0.3%; once
           armed the stop chases to that bar's close - $0.01. 1m bars cannot
           see a 1-cent chase between prints, so LIVE_NOW reads a little
           better than live.
"""
from __future__ import annotations

import collections
import json
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

COST = 0.20


def load():
    cache = rs._load_cache()
    out = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:
            continue
        sym, et, px = str(r.get("symbol") or ""), r.get("entry_time"), r.get("entry_price")
        if not rs.SYM_RE.match(sym) or not isinstance(et, (int, float)) or not px:
            continue
        day = bars.day_of(et)
        m = bars.et_minutes(et)
        if m < 9 * 60 + 31 or m > 15 * 60 + 30:
            continue
        B = cache.get((sym, day))
        if not B:
            continue
        i0 = bars.index_at(B[0], et)
        if i0 < 0 or i0 + 2 >= len(B[0]):
            continue
        out.append({"r": r, "B": B, "i0": i0, "px": float(px), "day": day})
    out.sort(key=lambda x: x["day"])
    return out


# ── 1. regret ───────────────────────────────────────────────────────────
def regret(fills):
    g = collections.defaultdict(list)
    for f in fills:
        r, B = f["r"], f["B"]
        xt, xp = r.get("exit_time"), r.get("exit_price")
        if not isinstance(xt, (int, float)) or not xp:
            continue
        j = bars.index_at(B[0], xt)
        if j < 0 or j + 5 >= len(B[0]):
            continue
        s = rs.score_path(B, j, float(xp))
        if not s:
            continue
        s["realized"] = (float(xp) / f["px"] - 1) * 100
        g[str(r.get("close_reason"))].append(s)
    print("== 1. REGRET: what price did AFTER we sold, from the exit price ==")
    print(f"  {'exit reason':<24}{'n':>5}{'realized':>10}{'+5m':>8}{'+15m':>8}{'+30m':>8}"
          f"{'best 30m':>10}{'ran ≥1%':>9}{'+1% first':>11}")
    for k, v in sorted(g.items(), key=lambda x: -len(x[1])):
        if len(v) < 8:
            continue
        m = lambda key: rs.mean([x[key] for x in v if x.get(key) is not None])
        ran = [x["up_30"] >= 1.0 for x in v if x.get("up_30") is not None]
        tp = [x["tp_1.0"] for x in v if x.get("tp_1.0") is not None]
        print(f"  {k:<24}{len(v):>5}{m('realized'):>+9.2f}%{m('ret_5'):>+7.2f}%"
              f"{m('ret_15'):>+7.2f}%{m('ret_30'):>+7.2f}%{m('up_30'):>+9.2f}%"
              f"{(sum(ran) / len(ran)) if ran else 0:>9.0%}{(rs.mean(tp) if tp else 0):>11.0%}")


# ── 2. replay ───────────────────────────────────────────────────────────
def walk(B, i0, px, *, stop_pct=1.0, arm_pct=None, chase=False, trail_pct=None,
         sixty=False, partial_pct=None, horizon=60):
    """Return (pct return, bars held)."""
    t, o, h, l, c, _v = B
    stop = px * (1 - stop_pct / 100)
    armed = arm_pct is None
    hi = px
    end = t[i0] + horizon * 60
    half_done, half_ret = False, 0.0
    last = i0
    for k in range(i0 + 1, len(t)):
        if t[k] > end:
            break
        last = k
        if l[k] <= stop:
            exit_ret = (min(stop, o[k]) / px - 1) * 100
            if half_done:
                return 0.5 * half_ret + 0.5 * exit_ret, k - i0
            return exit_ret, k - i0
        if sixty and k == i0 + 1 and h[k] <= px:
            return (c[k] / px - 1) * 100, 1
        if partial_pct is not None and not half_done and h[k] >= px * (1 + partial_pct / 100):
            half_done, half_ret = True, partial_pct
        hi = max(hi, h[k])
        if not armed and hi >= px * (1 + arm_pct / 100):
            armed = True
        if armed:
            if chase:
                stop = max(stop, c[k] - 0.01)
            if trail_pct is not None:
                stop = max(stop, hi * (1 - trail_pct / 100))
    exit_ret = (c[last] / px - 1) * 100
    if half_done:
        return 0.5 * half_ret + 0.5 * exit_ret, last - i0
    return exit_ret, last - i0


SHAPES = [
    ("LIVE_NOW (arm .3, chase, 60s)", dict(arm_pct=0.3, chase=True, sixty=True)),
    ("live without 60s rule", dict(arm_pct=0.3, chase=True)),
    ("arm .3, trail 0.5% from high", dict(arm_pct=0.3, trail_pct=0.5)),
    ("arm .3, trail 1% from high", dict(arm_pct=0.3, trail_pct=1.0)),
    ("arm .3, trail 1%, + 60s rule", dict(arm_pct=0.3, trail_pct=1.0, sixty=True)),
    ("stop 1%, trail 1% (no arm)", dict(trail_pct=1.0)),
    ("half at +0.5%, rest trail 1%", dict(partial_pct=0.5, trail_pct=1.0)),
    ("half at +1%, rest trail 1%", dict(partial_pct=1.0, trail_pct=1.0)),
    ("stop 1%, hold 30m", dict(horizon=30)),
    ("stop 1%, hold 60m", dict(horizon=60)),
    ("stop 2%, trail 1.5%, 60m", dict(stop_pct=2.0, trail_pct=1.5)),
]


def replay(fills):
    days = sorted({f["day"] for f in fills})
    mid = days[len(days) // 2]
    up30 = []
    for f in fills:
        s = rs.score_path(f["B"], f["i0"], f["px"])
        up30.append(s["up_30"] if s and s.get("up_30") is not None else None)
    print(f"\n== 2. REPLAY on {len(fills)} fills, {days[0]}..{days[-1]} (halves split at {mid}) ==")
    print(f"  {'shape':<32}{'gross':>8}{'±':>7}{'net':>8}{'win':>6}{'avg win':>9}{'avg loss':>9}"
          f"{'kept of run':>12}{'h1 / h2 net':>16}{'hold m':>8}")
    live = [f["r"] for f in fills]
    lr = [((x.get("realized_pl_usd") or 0) / (float(x["entry_price"]) * float(x["total_qty"])) * 100)
          for x in live if x.get("total_qty")]
    print(f"  {'ACTUAL live results':<32}{rs.mean(lr):>+7.3f}%{'':>7}{'':>8}"
          f"{sum(a > 0 for a in lr) / len(lr):>6.0%}")
    for name, kw in SHAPES:
        res = [walk(f["B"], f["i0"], f["px"], **kw) for f in fills]
        g = [a for a, _ in res]
        se = statistics.stdev(g) / math.sqrt(len(g))
        w = [a for a in g if a > 0]
        lo = [a for a in g if a <= 0]
        pairs = [(a, u) for a, u in zip(g, up30) if u is not None and u > 0]
        kept = sum(a for a, _ in pairs) / sum(u for _, u in pairs) if pairs else float("nan")
        h1 = [a - COST for (a, _), f in zip(res, fills) if f["day"] < mid]
        h2 = [a - COST for (a, _), f in zip(res, fills) if f["day"] >= mid]
        print(f"  {name:<32}{rs.mean(g):>+7.3f}%{se:>7.3f}{rs.mean(g) - COST:>+7.3f}%"
              f"{len(w) / len(g):>6.0%}{(rs.mean(w) if w else 0):>+8.2f}%{(rs.mean(lo) if lo else 0):>+8.2f}%"
              f"{kept:>12.0%}{rs.mean(h1):>+8.3f}/{rs.mean(h2):>+7.3f}"
              f"{statistics.median([b for _, b in res]):>8.0f}")


if __name__ == "__main__":
    fl = load()
    regret(fl)
    replay(fl)
