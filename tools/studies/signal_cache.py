#!/usr/bin/env python3
"""signal_cache - every desk-style pre-square signal per warehouse day, with its outcomes and its real SIP spread, built
once so later studies score in minutes instead of re-fetching (operator 11/19 'implement the ideas to speed up tests').

Signals: top-400 universe, $20-$400, up >= 1.5% vs prior close, minutes 15-360, refractory 30 (the BASE definition of
news_source / data_drops / movers_cap). Per signal: price, day %, 10-minute gross (open t+1 -> close t+10), desk bracket
gross (+20 / -50 bp within 15 min), and the spread key into the shared news_source quotes cache.

    .venv/bin/python tools/studies/signal_cache.py build --from 2026-04-28 --to 2026-11-18   # CPU only
    .venv/bin/python tools/studies/quotes_parallel.py signal_cache 2026-04-28..2026-11-18 3   # spreads, parallel
    .venv/bin/python tools/studies/signal_cache.py nightly                                  # today: build + spreads
    from signal_cache import load                                                           # rows with 'spread'
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "signal_cache"
T_FIRST, T_LAST, REFRACT = 15, 360, 30


def build_day(day_path, prev):
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, minute_grid, presquare_bars, first_true
    from holistic_entry import bracket, COST
    import news_source as ns
    day = load_day(day_path)
    rows = []
    if day["idx"].get("SPY") is None:
        return day, rows
    for s in universe(day, prev, set(ALL_ETF)):
        i, j = day["idx"][s], prev["idx"].get(s)
        if j is None:
            continue
        pc = ns.last_close(prev["c"][j])
        if not pc > 0:
            continue
        o, h, l, c = (day[k][i] for k in "ohlc")
        cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
        for t in first_true([t for t in presquare_bars(cc, hh, ll, off, 21) if T_FIRST <= t <= T_LAST], REFRACT):
            px = c[t]
            if not np.isfinite(px) or not (20 <= px <= 400) or (px / pc - 1) * 100 < 1.5:
                continue
            g10 = None
            if t + 10 < 390 and np.isfinite(o[t + 1]) and np.isfinite(c[t + 10]) and o[t + 1] > 0:
                g10 = round(float((c[t + 10] / o[t + 1] - 1) * 1e4), 2)
            b = bracket(o, h, l, c, t)
            rows.append({"day": day_path.stem, "sym": s, "t": int(t), "px": round(float(px), 2),
                         "day_pct": round(float((px / pc - 1) * 100), 2), "gross10": g10,
                         "bracket_gross": None if b is None else round(float(b) + COST, 2)})
    return day, rows


def build(a, b):
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    OUT.mkdir(parents=True, exist_ok=True)
    prev = None
    for k, fp in enumerate(files):
        if fp.stem < a or fp.stem > b:
            prev = None
            continue
        if prev is None and k > 0:
            prev = load_day(files[k - 1])
        out = OUT / f"{fp.stem}.json"
        if out.exists():
            prev = load_day(fp)
            continue
        day, rows = build_day(fp, prev)
        out.write_text(json.dumps(rows))
        print(fp.stem, len(rows), flush=True)
        prev = day


def plan_range(spec):
    a, _, b = spec.partition("..")
    rows = []
    for p in sorted(OUT.glob("20??-??-??.json")):
        if a <= p.stem <= (b or a):
            rows += json.loads(p.read_text())
    return sorted({("sc", r["day"], r["sym"], r["t"], 0.0) for r in rows})


def load(a, b=None):
    """Cached signals in [a, b] with 'spread' attached where the quote cache has it."""
    import news_source as ns
    cache = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    rows = []
    for p in sorted(OUT.glob("20??-??-??.json")):
        if a <= p.stem <= (b or a):
            for r in json.loads(p.read_text()):
                r["spread"] = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
                rows.append(r)
    return rows


def nightly():
    import news_source as ns
    from evening_family import WH_SIP
    last = sorted(WH_SIP.glob("20??-??-??.npz"))[-1].stem
    build(last, last)
    ns.fetch_quotes(plan_range(last))
    print("nightly", last, datetime.now(ET).isoformat(timespec="minutes"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("build", "nightly"))
    ap.add_argument("--from", dest="a", default="2026-04-28")
    ap.add_argument("--to", dest="b", default="2099-12-31")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.a, a.b)
    else:
        nightly()


if __name__ == "__main__":
    main()
