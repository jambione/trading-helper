#!/usr/bin/env python3
"""Side-by-side gallery of every support -> resistance range-trade match (operator 10/9: "show me the winners and the
losers"). Read-only; information only, no verdict. Same sample and rules as tools/studies/sr_range_trade.py (prereg
docs/studies/sr_range_trade_prereg.json): the first 1-min close up out of a charted support block, not inside
resistance, >= 0.40% room to the nearest charted resistance, fast %R below -20 and rising; buy the next open, target the
resistance bottom, stop on a close below the support bottom (sold at the next open), 30-min time stop, 0.20% cost.

Everything drawn for a match is what was known at the decision close: the charted blocks are those with
known_ts <= decision time, and each block carries its origin_ts (where TradingView starts the box) and known_ts (when a
live system could first see it), so the chart can show how late each box really appeared.

Run on the mini (data in ai_reports/indicator_levels): .venv/bin/python tools/studies/sr_range_gallery.py
Writes ai_reports/sr_range_gallery/gallery.json.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402
from sr_range_trade import COST, ET, HOLD, IND, ROOM_MIN, et_min, wr_fast  # noqa: E402

OUT = os.path.join(ROOT, "ai_reports", "sr_range_gallery")
BEFORE, AFTER = 45, 12          # bars shown before the decision and after the exit


def run_bracket_ex(rows, i_entry, entry, target, stop_px, day_end):
    """sr_range_trade.run_bracket, also returning the exit bar index, exit price and reason."""
    t0 = rows[i_entry][0]
    for j in range(i_entry, len(rows)):
        t, o, h, l, c = rows[j]
        if t >= day_end or t - t0 >= HOLD:
            if j == i_entry:
                return None
            return rows[j - 1][4] / entry - 1, j - 1, rows[j - 1][4], "time"
        if c < stop_px:
            px = rows[j + 1][1] if j + 1 < len(rows) else c
            return px / entry - 1, min(j + 1, len(rows) - 1), px, "stop"
        if h > target:
            return target / entry - 1, j, target, "target"
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    namedays = sorted({(r["sym"], r["day"]) for r in R["rows"] if r["pop"] == "C"})
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    out = []
    for sym, day in namedays:
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 100:
            continue
        h = [r[2] for r in rows]
        l = [r[3] for r in rows]
        c = [r[4] for r in rows]
        wr = wr_fast(h, l, c)
        states = list(OB.order_blocks(rows, bar_sec=60))
        day_idx = [i for i, r in enumerate(rows) if datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d") == day]
        if not day_idx:
            continue
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_end = d0.replace(hour=15, minute=55).timestamp()
        window = [i for i in day_idx if 9 * 60 + 40 <= et_min(rows[i][0] + 60) <= 15 * 60 and i + 1 < len(rows)]
        used = set()
        for i in window:
            tq = rows[i][0] + 60
            ch = OB.charted([b for b in states[i][1] if b.known_ts <= tq])
            sup = [b for b in ch if ((b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker))
                   and c[i - 1] <= b.top < c[i]]
            if not sup:
                continue
            S = max(sup, key=lambda b: b.top)
            key = (S.kind, round(S.top, 4), round(S.btm, 4), S.origin_ts)
            if key in used:
                continue
            used.add(key)
            resist = [b for b in ch if (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)]
            if any(b.btm <= c[i] <= b.top for b in resist):
                continue
            res = [b for b in resist if b.btm > c[i]]
            if not res:
                continue
            Rb = min(res, key=lambda b: b.btm)
            room = (Rb.btm / c[i] - 1) * 100
            if room < ROOM_MIN or not (wr[i] < -20 and wr[i] > wr[i - 1]):
                continue
            entry = rows[i + 1][1]
            r = run_bracket_ex(rows, i + 1, entry, Rb.btm, S.btm, day_end)
            if r is None:
                continue
            gross, j_exit, px_exit, why = r
            a = max(0, i - BEFORE)
            b = min(len(rows), max(j_exit, i + 1) + AFTER + 1)
            out.append({
                "sym": sym, "day": day, "t": tq, "room": room, "gross": gross, "net": gross - COST, "exit": why,
                "entry": entry, "exit_px": px_exit, "i_dec": i - a, "i_entry": i + 1 - a, "i_exit": j_exit - a,
                "bars": [[r_[0], r_[1], r_[2], r_[3], r_[4]] for r_ in rows[a:b]],
                "wr": [round(x, 2) for x in wr[a:b]],
                "support": {"top": S.top, "btm": S.btm, "origin": S.origin_ts, "known": S.known_ts},
                "resist": {"top": Rb.top, "btm": Rb.btm, "origin": Rb.origin_ts, "known": Rb.known_ts},
                "blocks": [{"kind": x.kind, "breaker": x.breaker, "top": x.top, "btm": x.btm, "origin": x.origin_ts,
                            "known": x.known_ts} for x in ch],
            })
    json.dump({"rules": __doc__, "cost": COST, "matches": out}, open(os.path.join(OUT, "gallery.json"), "w"))
    n = len(out)
    wins = sum(m["net"] > 0 for m in out)
    mean = sum(m["net"] for m in out) / n * 1e4 if n else float("nan")
    print(f"matches {n}; net winners {wins} ({wins / max(n, 1):.0%}); mean net {mean:+.1f} bp -> {OUT}/gallery.json")


if __name__ == "__main__":
    main()
