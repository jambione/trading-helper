#!/usr/bin/env python3
"""Does buying INSIDE (or just under) an overhead order block make the desk's square arms worse?
Pre-registration: docs/studies/order_block_gate_prereg.json (committed before this script is run).

Reuses the 2026-10-02 indicator-levels sample (no new outcomes are defined here):
  ai_reports/indicator_levels/rows.json  E = square arms ($10+, 09:40-15:30, 9/04-10/02, one per name per 15 min),
                                         F = desk fills ($10+, since 9/23); t = decision time, px = price at decision,
                                         net15 = 15-min outcome minus 0.20% round trip (as registered on 10/02)
  ai_reports/indicator_levels/ext.pkl    SIP 1-min bars with extended hours from 04:00, the day and the prior day
Feature (point-in-time, tools/order_blocks.py, LuxAlgo defaults: swing 10, wicks): RESIST = price at decision is
inside, or within 0.3% under, a resistance block KNOWN before the decision among the blocks LuxAlgo charts (last 3
per side, breakers included): an unbroken bearish OB, or a bullish breaker. Primary on 1-minute bars (the desk's
timeframe) with day fixed effects; 5-minute bars, "all blocks" and "inside only" are information.

Run on the mini after the close: .venv/bin/python tools/studies/order_block_gate.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import order_blocks as OB  # noqa: E402

IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "order_block_gate")
WITHIN = 0.3


def series(cache, sym, day, prev):
    rows = []
    for k in ((sym, prev), (sym, day)):
        df = cache.get(k)
        if df is None or not len(df):
            continue
        rows += [(t.timestamp(), float(r.open), float(r.high), float(r.low), float(r.close)) for t, r in df.iterrows()]
    rows.sort()
    return rows


def resample(rows, sec):
    out, cur = [], None
    for t, o, h, l, c in rows:
        b = t - t % sec
        if cur is None or cur[0] != b:
            if cur:
                out.append(tuple(cur))
            cur = [b, o, h, l, c]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
    if cur:
        out.append(tuple(cur))
    return out


def flags_for(rows, bar_sec, queries):
    """queries: list of (t_decision, price). Returns (charted within, charted inside, all-blocks within) or None."""
    if not rows:
        return [None] * len(queries)
    states = []                                   # (bar close time, blocks)
    for i, blocks in OB.order_blocks(rows, bar_sec=bar_sec):
        states.append((rows[i][0] + bar_sec, blocks))
    closes = [s[0] for s in states]
    out = []
    for tq, px in queries:
        j = int(np.searchsorted(closes, tq, side="right")) - 1   # last bar closed at or before the decision
        if j < 0 or px is None:
            out.append(None)
            continue
        allb = [b for b in states[j][1] if b.known_ts <= tq]
        w = OB.overhead_resistance(OB.charted(allb), px, WITHIN)
        ins = [b for b in w if b.btm <= px <= b.top]
        out.append((bool(w), bool(ins), bool(OB.overhead_resistance(allb, px, WITHIN))))
    return out


def fe(y, g, day):
    """Demean y and g within day (day fixed effects)."""
    my, mg = collections.defaultdict(list), collections.defaultdict(list)
    for v, x, d in zip(y, g, day):
        my[d].append(v)
        mg[d].append(x)
    my = {d: statistics.fmean(v) for d, v in my.items()}
    mg = {d: statistics.fmean(v) for d, v in mg.items()}
    return [v - my[d] for v, d in zip(y, day)], [x - mg[d] for x, d in zip(g, day)]


def fe_diff(y, g, day):
    """Slope of y on g after day fixed effects, day-clustered (CR1) SE."""
    yd, gd = fe(y, g, day)
    sxx = sum(x * x for x in gd)
    if sxx <= 0:
        return None, None
    b = sum(a * x for a, x in zip(yd, gd)) / sxx
    by = collections.defaultdict(float)
    for a, x, d in zip(yd, gd, day):
        by[d] += x * (a - b * x)
    G, n = len(by), len(y)
    se = math.sqrt(sum(v * v for v in by.values()) / sxx ** 2 * G / (G - 1) * (n - 1) / (n - 2))
    return b, se


def cl_diff(y, g, day):
    n = len(y)
    m0 = statistics.fmean([y[i] for i in range(n) if not g[i]])
    b = statistics.fmean([y[i] for i in range(n) if g[i]]) - m0
    gbar = sum(g) / n
    sxx = sum((x - gbar) ** 2 for x in g)
    by = collections.defaultdict(float)
    for i in range(n):
        by[day[i]] += (g[i] - gbar) * (y[i] - m0 - b * g[i])
    G = len(by)
    se = math.sqrt(sum(v * v for v in by.values()) / sxx ** 2 * G / (G - 1) * (n - 1) / (n - 2))
    return b, se


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    days = R["days"]
    half = {d: "AB"[k % 2] for k, d in enumerate(days)}
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    rows = [r for r in R["rows"] if r["pop"] in ("E", "F") and r.get("net15") is not None]
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["sym"], r["day"])].append(r)
    miss = collections.Counter()
    for (sym, day), rs_ in by.items():
        one = series(cache, sym, day, prev_of.get(day))
        if not one:
            miss["no_bars"] += len(rs_)
            continue
        q = [(r["t"], r.get("px")) for r in rs_]
        for tf, bars_, sec in (("1m", one, 60), ("5m", resample(one, 300), 300)):
            for r, f in zip(rs_, flags_for(bars_, sec, q)):
                r[f"ob_{tf}"] = f
                if f is None:
                    miss[f"no_flag_{tf}_{r['pop']}"] += 1
    json.dump(rows, open(os.path.join(OUT, "rows.json"), "w"))
    lines = [f"# Order-block gate (prereg docs/studies/order_block_gate_prereg.json)", "",
             f"days {days[0]}..{days[-1]} ({len(days)}), halves alternate days; rows E {sum(r['pop'] == 'E' for r in rows)}, "
             f"F {sum(r['pop'] == 'F' for r in rows)}; missing bars {dict(miss)}", ""]
    verdict = {}
    for pop in ("E", "F"):
        for tf in ("1m", "5m"):
            for k, lab in ((0, f"within {WITHIN}%"), (1, "inside only"), (2, f"ALL blocks within {WITHIN}%")):
                cell = f"{pop} {tf} {lab}"
                lines.append(f"## {cell}")
                res = {}
                for h in ("A", "B", "all"):
                    xs = [r for r in rows if r["pop"] == pop and r.get(f"ob_{tf}") is not None and (h == "all" or half[r["day"]] == h)]
                    y = [r["net15"] * 1e4 for r in xs]
                    g = [1 if r[f"ob_{tf}"][k] else 0 for r in xs]
                    nf = sum(g)
                    if nf < 5 or nf == len(g) or len({r["day"] for r in xs}) < 3:
                        lines.append(f"- half {h}: flagged {nf} of {len(g)} (too few)")
                        res[h] = None
                        continue
                    dd = [r["day"] for r in xs]
                    b, se = fe_diff(y, g, dd)
                    b0, se0 = cl_diff(y, g, dd)
                    fy = [v for v, gg in zip(y, g) if gg]
                    ry = [v for v, gg in zip(y, g) if not gg]
                    wz = lambda v: max(-300.0, min(300.0, v))
                    bw, _ = fe_diff([wz(v) for v in y], g, dd)
                    real = ""
                    if pop == "F":
                        fr = [r["realized"] * 1e4 for r, gg in zip(xs, g) if gg and r.get("realized") is not None]
                        rr = [r["realized"] * 1e4 for r, gg in zip(xs, g) if not gg and r.get("realized") is not None]
                        if fr and rr:
                            real = f" | realized flagged {statistics.fmean(fr):+.1f} vs rest {statistics.fmean(rr):+.1f}"
                    lines.append(f"- half {h}: flagged {nf}/{len(g)} ({nf / len(g):.0%}) days {len(set(dd))} | net15 {statistics.fmean(fy):+.1f} "
                                 f"vs rest {statistics.fmean(ry):+.1f} (medians {statistics.median(fy):+.1f} / {statistics.median(ry):+.1f})"
                                 f" | DAY-FE diff {b:+.1f} bp, t {b / se:+.2f} | raw diff {b0:+.1f} (t {b0 / se0:+.2f}) | winsorized ±300 FE diff {bw:+.1f}{real}")
                    res[h] = (b, b / se, nf)
                verdict[cell] = res
                lines.append("")
    p = verdict["E 1m within 0.3%"]
    if any(p[h] is None or p[h][2] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 flagged arms in a half): no verdict"
    elif all(p[h][0] <= -5 and p[h][1] <= -2 for h in ("A", "B")):
        v = "PASS (earns the >= 10-session held-out confirmation; not a gate yet)"
    else:
        v = "FAIL (= no large effect; a small one cannot be ruled out)"
    f = verdict["F 1m within 0.3%"]
    f_same = all(f[h] is not None and f[h][0] < 0 for h in ("A", "B"))
    lines.append(f"**PRIMARY (E, 1m, charted blocks, within 0.3%, day fixed effects): {v}** (bar: diff <= -5 bp, t <= -2, flagged n >= 50, both halves)")
    lines.append(f"Fills, same sign both halves: {'yes' if f_same else 'no'} (information)")
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
