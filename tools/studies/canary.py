#!/usr/bin/env python3
"""Planted-edge canary for today's study harnesses (operator 10/23: 'does everything fail at a few bp because of a
bug?'). For each harness, the REAL study functions run twice on the same real bars: once as-is and once with every
price AFTER each trade's entry multiplied by (1 + STEP). A harness that reads returns correctly must report the
trade's gain higher by about STEP, and its lift (trade minus control / minus the cross-section) higher by STEP times
the share of the effect the control does not also receive. A harness that shrinks real effects shows up here.

  .venv/bin/python tools/studies/canary.py [--days N]      (mini; reads the warehouse and the etf_minute cache)

Expected deltas (STEP = +10 bp):
  eod_reversal  lift  = 10 x (1 - k/n) ~ 9.0 bp (losers are a decile of the cross-section mean)
  periodicity   lift  = 10 x (1 - k/n) ~ 9.0 bp per slot
  fib_pullback  trade ~ +10 bp; control +10 only when its window spans the step (control can precede the trade)
  fence_bar     trade ~ +10 bp; control 0 (drawn after the trigger) -> lift ~ +10
  anchor        trade ~ +10 bp; control 0 -> lift ~ +10
Bracket exits (stop / target) make the trade delta path-dependent: a step away from the stop can save a stop-out
(> +10) and a target can cap it (< +10). The time-exit subset must show exactly +10.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STEP = 0.001


def bump_rows(r: np.ndarray, from_minute: int) -> np.ndarray:
    """Copy of minute rows [m, o, h, l, c, ...] with o/h/l/c of rows at or after from_minute scaled by 1 + STEP."""
    out = np.array(r, dtype=float, copy=True)
    m = out[:, 0] >= from_minute
    out[m, 1:5] *= 1 + STEP
    return out


def bump_list(bars: list, from_index: int) -> list:
    return [b if i < from_index else (b[0],) + tuple(x * (1 + STEP) for x in b[1:5]) + tuple(b[5:])
            for i, b in enumerate(bars)]


def summary(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "mean_bp": round(1e4 * statistics.mean(xs), 2) if xs else None,
            "min_bp": round(1e4 * min(xs), 2) if xs else None, "max_bp": round(1e4 * max(xs), 2) if xs else None}


# ── eod_reversal ─────────────────────────────────────────────────────────────

def canary_eod(days: list[str]) -> dict:
    import eod_reversal as E
    import indicator_confirm_hist as H
    allw = H.warehouse_days()
    d_lift, expect = [], []
    for day in days:
        i = allw.index(day)
        today, prev = H.load_raw("sip", day), H.load_raw("sip", allw[i - 1])
        names = [x for x in (E.name_row(s, r, prev.get(s))[0] for s, r in today.items()) if x]
        if len(names) < E.MIN_ELIGIBLE:
            continue
        real = E.day_summary({"day": day, "names": names, "spy_lh": None, "spy_rod3": None})
        los = {r["s"] for r in E.bottom(names, E.DECILE)}
        planted = [E.name_row(r["s"], bump_rows(today[r["s"]], E.M_IN + 1), prev.get(r["s"]))[0]
                   if r["s"] in los else r for r in names]
        pl = E.day_summary({"day": day, "names": planted, "spy_lh": None, "spy_rod3": None})
        d_lift.append(pl["lift"] - real["lift"])
        expect.append(STEP * (1 - len(los) / len(names)))
    return {"lift_delta": summary(d_lift), "expected": summary(expect)}


# ── periodicity ──────────────────────────────────────────────────────────────

def canary_periodicity(days: list[str]) -> dict:
    import periodicity as P
    import indicator_confirm_hist as H
    from collections import defaultdict
    allw = [d for d in H.warehouse_days() if d <= max(days)]
    history = defaultdict(lambda: defaultdict(list))
    d_lift, expect = [], []
    for cur, day in enumerate(allw):
        raw = H.load_raw("sip", day)
        f = {s: P.slot_fields(r) for s, r in raw.items()}
        if day in days:
            for k in range(1, 13):
                elig = []
                for s, x in f.items():
                    if x["ent"][k] is None or x["ent"][k] < P.MIN_PX:
                        continue
                    sig = P.signals(history, s, k, cur)
                    if sig is not None:
                        elig.append((sig, s, x["ret"][k]))
                if len(elig) < P.MIN_ELIGIBLE:
                    continue
                n = max(1, int(len(elig) * P.DECILE))
                top = {e[1] for e in sorted(elig, key=lambda e: (-e[0], e[1]))[:n]}
                real = P._lift(elig)
                elig_p = [(e[0], e[1], P.slot_fields(bump_rows(raw[e[1]], P.SLOT_START[k] + 1))["ret"][k]
                           if e[1] in top else e[2]) for e in elig]
                d_lift.append(P._lift(elig_p) - real)
                expect.append(STEP * (1 - n / len(elig)))
        for s, x in f.items():
            for k in range(13):
                history[s][k].append((cur, x["hist"][k]))
    return {"slot_lift_delta": summary(d_lift), "expected": summary(expect)}


# ── fib_pullback ─────────────────────────────────────────────────────────────

def canary_fib(days: list[str]) -> dict:
    import fib_pullback as F
    import indicator_confirm_hist as H
    allw = H.warehouse_days()
    d_trade, d_ctrl, d_time = [], [], []
    for day in days:
        raw = H.load_raw("sip", day)
        p_iex = H.load_raw("iex", allw[allw.index(day) - 1])
        for sym in H.sample_names(day, [s for s in H.load_raw("iex", day) if s in raw and s in p_iex]):
            bars = [(int(r[0]),) + tuple(float(x) for x in r[1:]) for r in raw[sym]]
            for sig in F.detect(bars, "GP"):
                tr = F.trade(bars, sig)
                if tr is None or "skip" in tr:
                    continue
                pb = bump_list(bars, sig["entry_i"] + 1)          # every bar after the entry bar
                tp = F.trade(pb, sig)
                if tp is None or "skip" in tp:
                    continue
                d_trade.append(tp["gross"] - tr["gross"])
                if tr["why"] == "time" and tp["why"] == "time":
                    d_time.append(tp["gross"] - tr["gross"])
                c0 = F.control(bars, sig, tr, sym, day, "fib-gp-ctrl")
                c1 = F.control(pb, sig, tp, sym, day, "fib-gp-ctrl")
                if c0 and c1:
                    d_ctrl.append(c1["gross"] - c0["gross"])
    return {"trade_delta": summary(d_trade), "time_exit_trade_delta": summary(d_time),
            "control_delta": summary(d_ctrl)}


# ── fence_bar and anchor_power_bar (etf_bars) ────────────────────────────────

def canary_etf(lo: str, hi: str) -> dict:
    import etf_bars as B
    import fence_bar as Fe
    import anchor_power_bar as A
    from collections import deque
    out = {}
    for study in ("fence_bar", "anchor_power_bar"):
        d_trade, d_ctrl, d_time = [], [], []
        for sym in ("SPY", "QQQ"):
            closes5, closes2, size = deque(maxlen=20), deque(maxlen=20), B.SizeRef()
            for day, r_all in B.load(sym).items():
                if not B.in_scope(day) or not B.is_full_session(r_all, day):
                    continue
                r = B.rth(r_all)
                if study == "fence_bar":
                    bars5 = B.clock_bars(r, 5, 4)
                    sma = []
                    for b in bars5:
                        if b is not None:
                            closes5.append(b[3])
                        sma.append(statistics.mean(closes5) if len(closes5) == 20 else None)
                    if not (lo <= day <= hi):
                        continue
                    sig = Fe.find_trade(bars5, sma, 1)
                    if not sig or not sig["sma_ok"]:
                        continue
                    t0 = Fe.trade(r, sig, 1, sym, day)
                    e = B.first_after(r, sig["end"])
                    if "skip" in t0 or e is None:
                        continue
                    t1 = Fe.trade(bump_rows(r, int(r[e, 0]) + 1), sig, 1, sym, day)
                    if "skip" in t1:
                        continue
                    d_trade.append(t1["gross"] - t0["gross"])
                    if t0["why"] == "time" and t1["why"] == "time":
                        d_time.append(t1["gross"] - t0["gross"])
                    if t0["ctrl"] is not None and t1["ctrl"] is not None:
                        d_ctrl.append(t1["ctrl"] - t0["ctrl"])
                else:
                    bars2 = B.clock_bars(r, 2, 2)
                    sma = []
                    for b in bars2:
                        if b is not None:
                            closes2.append(b[3])
                        sma.append(statistics.mean(closes2) if len(closes2) == 20 else None)
                    if lo <= day <= hi:
                        for k, b in A.candidates(bars2, sma, size):
                            e = B.first_after(r, b[4])
                            if e is None:
                                continue
                            o, stop = float(r[e, 1]), b[2] - B.TICK
                            if o <= stop:
                                continue
                            tgt = o + 2 * (o - stop)
                            sp, tp = (o - stop) / o, 2 * (o - stop) / o
                            rp = bump_rows(r, int(r[e, 0]) + 1)
                            s0, s1 = B.simulate(r, e, 1, stop, tgt), B.simulate(rp, e, 1, stop, tgt)
                            c0 = B.control(r, sym, day, b[4], min(b[4] + 60, A.CTRL_END), 1, sp, tp, f"apb-ctrl|{k}")
                            c1 = B.control(rp, sym, day, b[4], min(b[4] + 60, A.CTRL_END), 1, sp, tp, f"apb-ctrl|{k}")
                            d_trade.append(s1["gross"] - s0["gross"])
                            if s0["why"] == "time" and s1["why"] == "time":
                                d_time.append(s1["gross"] - s0["gross"])
                            if c0 and c1:
                                d_ctrl.append(c1["gross"] - c0["gross"])
                            break                             # first candidate per day is enough for the canary
                    size.add(bars2)
        out[study] = {"trade_delta": summary(d_trade), "time_exit_trade_delta": summary(d_time),
                      "control_delta": summary(d_ctrl)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=12)
    a = ap.parse_args()
    import indicator_confirm_hist as H
    wd = [d for d in H.warehouse_days() if "2026-05-11" <= d <= "2026-10-15"]
    pick = wd[:: max(1, len(wd) // a.days)][: a.days]
    res = {"step_bp": STEP * 1e4, "days": pick,
           "eod_reversal": canary_eod(pick),
           "periodicity": canary_periodicity(pick),
           "fib_pullback": canary_fib(pick[: max(4, a.days // 3)]),
           **canary_etf("2023-01-01", "2024-12-31")}
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
