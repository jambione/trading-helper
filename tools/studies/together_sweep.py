#!/usr/bin/env python3
"""'Trending together' %R rule grid with a held-out confirmation. Spec: docs/studies/together_sweep_prereg.json.

Operator 10/17: strict rule set - both lines dipped, close together, rising together - entered at the EARLIEST bar the
rule triggers, and every variation of the thresholds. 243 cells (3^5). Discovery = half A sessions (pick the best
cell); confirmation = half B sessions, never used for the pick. Data, sample, warm-up, markout, controls and cost are
those of prescreen_turn / early_entry_honest (warehouse, 400 seeded names/session, IEX lines, SIP markouts, later-only
same-name control, same-minute cross-name control, 10 bp).

  .venv/bin/python tools/studies/together_sweep.py --score [--workers 6]
"""
from __future__ import annotations

import argparse
import itertools
import math
import os
import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402

ROOT = os.environ.get("REPO") or os.getcwd()
DIP_LOOK = 20
GRID = {"dip_fast": (-90.0, -80.0, -70.0), "dip_slow": (-60.0, -50.0, -40.0), "gap": (10.0, 20.0, 30.0),
        "rise_bars": (2, 3, 4), "early": (-50.0, -30.0, -10.0)}
BASE = (-80.0, -50.0, 20.0, 3, -30.0)        # the operator's stated defaults, reported on its own
CELLS = list(itertools.product(*GRID.values()))
MIN_A_SIGNALS, MIN_A_DAYS = 300, 30
EPS = E.EPS


def _runlen(rise: np.ndarray) -> np.ndarray:
    out = np.zeros(len(rise), np.int32)
    run = 0
    for i, r in enumerate(rise):
        run = run + 1 if r else 0
        out[i] = run
    return out


def cell_flags(fast: np.ndarray, slow: np.ndarray, cell: tuple, pre: dict | None = None) -> np.ndarray:
    """Bar-close flag for one cell, completed bars only (bar i uses bars <= i):
    dip: min(fast over i-20..i) <= dip_fast AND min(slow over i-20..i) <= dip_slow;
    close: |fast - slow| <= gap; together: both lines rose (> previous + 0.05) on each of the last rise_bars bars;
    early: fast <= early."""
    dip_fast, dip_slow, gap, k, early = cell
    pre = pre if pre is not None else prep(fast, slow)
    with np.errstate(invalid="ignore"):
        return ((pre["minf"] <= dip_fast) & (pre["mins"] <= dip_slow) & (np.abs(fast - slow) <= gap)
                & (pre["rlf"] >= k) & (pre["rls"] >= k) & (fast <= early))


def prep(fast: np.ndarray, slow: np.ndarray) -> dict:
    import pandas as pd
    with np.errstate(invalid="ignore"):
        rf = np.r_[False, fast[1:] > fast[:-1] + EPS]
        rs = np.r_[False, slow[1:] > slow[:-1] + EPS]
    return {"minf": pd.Series(fast).rolling(DIP_LOOK + 1, min_periods=DIP_LOOK + 1).min().to_numpy(),
            "mins": pd.Series(slow).rolling(DIP_LOOK + 1, min_periods=DIP_LOOK + 1).min().to_numpy(),
            "rlf": _runlen(rf), "rls": _runlen(rs)}


def onsets(minutes: list[int], flag: np.ndarray) -> list[int]:
    """EARLIEST trigger: flag true and the previous bar false, in 09:50-15:30, 15-minute refractory."""
    out, last = [], None
    for i in np.flatnonzero(flag):
        m = minutes[i]
        if i > 0 and flag[i - 1]:
            continue
        if not (E.WIN_START <= m <= E.WIN_END):
            continue
        if last is not None and m - last < E.REFRACT:
            continue
        out.append(m)
        last = m
    return out


def day_job(args) -> tuple[str, dict]:
    """Per day and cell: [n, sum lift, sum gross, n cross, sum cross lift]."""
    day, prev = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    board, iex, sip_f, prior = lz["board"], lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    acc = {c: [0, 0.0, 0.0, 0, 0.0] for c in CELLS}
    for sym in sorted(board):
        if sym not in iex or sym not in sips or len(iex[sym]) < 30:
            continue
        try:
            ser = E.name_day_series(iex[sym], prior.get(sym))
        except Exception:  # noqa: BLE001
            continue
        f, s = np.array(ser["fast"], float), np.array(ser["slow"], float)
        pre = prep(f, s)
        sip = sips[sym]
        for c in CELLS:
            for sm in onsets(ser["minutes"], cell_flags(f, s, c, pre)):
                em = sm + 1
                g = E.markout(sip, em)
                km = E.control_minute(sym, day, "TOGETHER", sip, board[sym], em)
                if g is None or km is None:
                    continue
                gc = E.markout(sip, km)
                a = acc[c]
                a[0] += 1; a[1] += g - gc; a[2] += g
                xc = E.cross_control(sym, day, "TOGETHER", board, sips, sm)
                if xc is not None:
                    a[3] += 1; a[4] += g - xc
    return day, acc


def _de(vals: list[float]) -> tuple[float | None, float | None, int]:
    if not vals:
        return None, None, 0
    mu = st.fmean(vals)
    if len(vals) < 3 or st.stdev(vals) == 0:
        return mu, None, len(vals)
    return mu, mu / (st.stdev(vals) / math.sqrt(len(vals))), len(vals)


def summarize(per_day: dict[str, dict], days: list[str], cut: str) -> dict:
    out = {}
    for c in CELLS:
        rec = {}
        for h, ds in (("A", [d for d in days if d <= cut]), ("B", [d for d in days if d > cut])):
            lifts = [per_day[d][c][1] / per_day[d][c][0] for d in ds if per_day[d][c][0]]
            gross = [per_day[d][c][2] / per_day[d][c][0] for d in ds if per_day[d][c][0]]
            cross = [per_day[d][c][4] / per_day[d][c][3] for d in ds if per_day[d][c][3]]
            mu, t, nd = _de(lifts)
            gm = _de(gross)[0]
            rec[h] = {"lift": mu, "t": t, "days": nd, "n": sum(per_day[d][c][0] for d in ds),
                      "net10": None if gm is None else gm - E.COST, "net3": None if gm is None else gm - 0.0003,
                      "cross": _de(cross)[0]}
        out[c] = rec
    return out


def pick(summary: dict) -> tuple | None:
    ok = [c for c, r in summary.items() if r["A"]["n"] >= MIN_A_SIGNALS and r["A"]["days"] >= MIN_A_DAYS
          and r["A"]["lift"] is not None]
    return max(ok, key=lambda c: (summary[c]["A"]["lift"], c)) if ok else None


def verdict(b: dict) -> str:
    if b["n"] < E.MIN_SIGNALS or b["days"] < E.MIN_DAYS or b["lift"] is None or b["t"] is None:
        return "UNDERPOWERED"
    if (b["lift"] >= E.LIFT_BAR and b["t"] >= 2.0 and b["net10"] is not None and b["net10"] > 0
            and b["cross"] is not None and b["cross"] > 0):
        return "PASS"
    if (b["lift"] > 0 and b["t"] >= 1.5 and b["net3"] is not None and b["net3"] > 0
            and b["cross"] is not None and b["cross"] > 0):
        return "PROMISING"
    return "FAIL"


def _rank_corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx = {v: i for i, v in enumerate(sorted(range(len(xs)), key=lambda i: xs[i]))}
    ry = {v: i for i, v in enumerate(sorted(range(len(ys)), key=lambda i: ys[i]))}
    a = [rx[i] for i in range(len(xs))]
    b = [ry[i] for i in range(len(ys))]
    ma, mb = st.fmean(a), st.fmean(b)
    num = sum((p - ma) * (q - mb) for p, q in zip(a, b))
    den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))
    return None if den == 0 else num / den


def report(summary: dict, cut: str, ndays: int) -> str:
    bp = lambda x: "-" if x is None else f"{x * 1e4:+.1f}"  # noqa: E731
    tt = lambda x: "-" if x is None else f"{x:+.2f}"  # noqa: E731
    best = pick(summary)
    L = ["# 'Trending together' %R grid (together_sweep_prereg)", "",
         f"{len(CELLS)} cells; {ndays} sessions; discovery = half A (<= {cut}), confirmation = half B (held out).", ""]
    if best is None:
        return "\n".join(L + ["No cell met the discovery minimums: UNDERPOWERED."])
    b = summary[best]["B"]
    v = verdict(b)
    L += [f"**Picked on A:** dip_fast {best[0]}, dip_slow {best[1]}, gap {best[2]}, rise_bars {best[3]}, early {best[4]}"
          f" (A lift {bp(summary[best]['A']['lift'])} bp, t {tt(summary[best]['A']['t'])}, n {summary[best]['A']['n']})",
          f"**Held-out B:** lift {bp(b['lift'])} bp (t {tt(b['t'])}), n {b['n']}, {b['days']} days, net@10 "
          f"{bp(b['net10'])}, net@3 {bp(b['net3'])}, cross {bp(b['cross'])} -> **{v}**", ""]
    bs = summary[BASE]
    L += [f"Operator default {BASE}: A lift {bp(bs['A']['lift'])} (t {tt(bs['A']['t'])}, n {bs['A']['n']}); "
          f"B lift {bp(bs['B']['lift'])} (t {tt(bs['B']['t'])}, n {bs['B']['n']}, cross {bp(bs['B']['cross'])}) -> "
          f"{verdict(bs['B'])}", ""]
    valid = [c for c in CELLS if summary[c]["A"]["lift"] is not None and summary[c]["B"]["lift"] is not None]
    rc = _rank_corr([summary[c]["A"]["lift"] for c in valid], [summary[c]["B"]["lift"] for c in valid])
    pos = sum(1 for c in valid if summary[c]["B"]["lift"] > 0)
    L += [f"Grid stability: rank correlation of A vs B lift across {len(valid)} cells {tt(rc)}; "
          f"cells with B lift > 0: {pos}/{len(valid)}", "", "Top 5 cells on A, with their held-out B:", "",
          "| cell | A lift (t, n) | B lift (t, n) | B cross | B verdict |", "|---|---:|---:|---:|---|"]
    top = sorted([c for c in valid if summary[c]["A"]["n"] >= MIN_A_SIGNALS],
                 key=lambda c: summary[c]["A"]["lift"], reverse=True)[:5]
    for c in top:
        a, bb = summary[c]["A"], summary[c]["B"]
        L.append(f"| {c} | {bp(a['lift'])} ({tt(a['t'])}, {a['n']}) | {bp(bb['lift'])} ({tt(bb['t'])}, {bb['n']}) | "
                 f"{bp(bb['cross'])} | {verdict(bb)} |")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    lz = P.usable_days()
    days = [d["day"] for d in lz]
    cut = E.split(days)
    print(f"sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; A ends {cut}; "
          f"{len(CELLS)} cells; {P.SAMPLE} names/session", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    jobs = [(d["day"], d._prev) for d in lz]
    per_day = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (day, acc) in enumerate(ex.map(day_job, jobs)):
            per_day[day] = acc
            if k % 10 == 0:
                print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
    md = report(summarize(per_day, days, cut), cut, len(days))
    print(md)
    with open(os.path.join(ROOT, "ai_reports", "warehouse", "together_sweep_result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
