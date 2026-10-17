#!/usr/bin/env python3
"""Few trending names, kept all day, buy their %R dips. Spec: docs/studies/trend_dip_prereg.json.

Operator 10/17: 'keep a few names and just keep an eye on them ... a few volatile names that are trending - watching
their dips and buying the trends based on the %R', instead of find -> book -> wait-for-arm (which enters at the tail).

Each session (warehouse, the 119 registered sessions): at 10:30, rank ALL liquid names by day change using SIP bars
through the 10:29 close, keeping names above their 09:30-09:59 opening-range high and up >= TREND_MIN. The top N are
watched 10:31-15:30. Entry = the EARLIEST bar of a dip-and-turn (fast %R <= dip on one of the last DIP_BARS bars, fast
rising now, the IEX close still above the prior close), next-minute SIP open, 10-minute hold, 15-minute refractory.
Grid N x dip (9 cells), picked on half A, confirmed on held-out half B (as together_sweep).

Information: the same dip rule on a seeded 100-name comparison sample (does selection matter?); random minutes in the
trending names vs the comparison names (does the trend persist?); a 10:31 -> close hold of the trending names (the
day-hold idea).

  .venv/bin/python tools/studies/trend_dip_sweep.py --score [--workers 6]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import os
import random
import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402
import together_sweep as T  # noqa: E402

ROOT = os.environ.get("REPO") or os.getcwd()
PICK_MIN = 10 * 60 + 30            # selection uses bars through 10:29 (closed by 10:30)
WATCH_START, WATCH_END = 10 * 60 + 31, 15 * 60 + 30
OR_END = 9 * 60 + 59
TREND_MIN = 0.02
DIP_BARS = 5
COMPARE_N = 100
GRID = {"top_n": (3, 5, 10), "dip": (-90.0, -80.0, -70.0)}
CELLS = list(itertools.product(*GRID.values()))
RULE = "TRENDDIP"
LAST_DAY, N_DAYS, FIRST_DAY, CUT = "2026-10-15", 119, "2026-04-28", "2026-07-23"


def select_trending(sip_today: dict, sip_prev: dict, n_max: int = 10) -> list[str]:
    """Top names by day change at 10:30, using only SIP bars through 10:29. Must be above the opening-range high
    (09:30-09:59) and up >= TREND_MIN versus the previous session's last close."""
    rows = []
    for s, f in sip_today.items():
        p = sip_prev.get(s)
        if p is None or not len(p):
            continue
        prev_close = float(p["close"].iloc[-1])
        m = [E._et_min(t.timestamp()) for t in f["time"]]
        upto = [i for i, x in enumerate(m) if x < PICK_MIN]
        orng = [i for i, x in enumerate(m) if x <= OR_END]
        if not upto or not orng or prev_close <= 0:
            continue
        last = float(f["close"].iloc[upto[-1]])
        or_high = float(f["high"].iloc[orng].max())
        chg = last / prev_close - 1
        if chg >= TREND_MIN and last > or_high:
            rows.append((chg, s, prev_close))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [s for _c, s, _p in rows[:n_max]]


def dip_onsets(minutes: list[int], fast: np.ndarray, close: np.ndarray, prev_close: float, dip: float) -> list[int]:
    """EARLIEST bar of a dip-and-turn: fast <= dip on one of the last DIP_BARS bars (incl. this one), fast rising now
    (> previous + eps), IEX close above the prior session close; 10:31-15:30, 15-minute refractory."""
    import pandas as pd
    with np.errstate(invalid="ignore"):
        lo = pd.Series(fast).rolling(DIP_BARS, min_periods=1).min().to_numpy()
        rise = np.r_[False, fast[1:] > fast[:-1] + E.EPS]
        flag = (lo <= dip) & rise & (close > prev_close)
    out, last = [], None
    for i in np.flatnonzero(flag):
        if i > 0 and flag[i - 1]:
            continue
        m = minutes[i]
        if not (WATCH_START <= m <= WATCH_END) or (last is not None and m - last < E.REFRACT):
            continue
        out.append(m)
        last = m
    return out


def _seeded(day: str, tag: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{day}|{tag}".encode()).digest()[:8], "big"))


def day_job(args) -> tuple[str, dict]:
    day, prev = args
    E.WARM_LINES = True
    wh = P.WH
    sip_t = P._frames(os.path.join(wh, "sip", f"{day}.npz"))
    sip_p = P._frames(os.path.join(wh, "sip", f"{prev}.npz"))
    iex_t = P._frames(os.path.join(wh, "iex", f"{day}.npz"))
    iex_p = P._frames(os.path.join(wh, "iex", f"{prev}.npz"))
    top = [s for s in select_trending(sip_t, sip_p) if s in iex_t and s in iex_p]
    pool = sorted(s for s in sip_t if s in iex_t and s in iex_p and s in sip_p and s not in top)
    comp = sorted(_seeded(day, "trend-compare").sample(pool, min(COMPARE_N, len(pool))))
    names = top + comp
    sips = {s: E.sip_map(sip_t[s]) for s in names}
    board = {s: set(range(E.RTH_START, E.RTH_END)) for s in names}
    pclose = {s: float(sip_p[s]["close"].iloc[-1]) for s in names}
    acc = {c: [0, 0.0, 0.0, 0, 0.0] for c in CELLS}
    info = {"cmp_dip": {d: [0, 0.0] for d in GRID["dip"]}, "rnd_top": [0, 0.0], "rnd_cmp": [0, 0.0],
            "hold_top": [0, 0.0], "hold_cmp": [0, 0.0], "n_top": len(top)}
    for s in names:
        try:
            ser = E.name_day_series(iex_t[s], iex_p[s].tail(E.CON_PRIOR_BARS).reset_index(drop=True))
        except Exception:  # noqa: BLE001
            continue
        f, c, m = np.array(ser["fast"], float), np.array(ser["close"], float), ser["minutes"]
        sip = sips[s]
        rank = top.index(s) if s in top else None
        for dip in GRID["dip"]:
            for sm in dip_onsets(m, f, c, pclose[s], dip):
                em = sm + 1
                g = E.markout(sip, em)
                km = E.control_minute(s, day, RULE, sip, board[s], em)
                if g is None or km is None:
                    continue
                gc = E.markout(sip, km)
                if rank is None:
                    info["cmp_dip"][dip][0] += 1
                    info["cmp_dip"][dip][1] += g - gc
                    continue
                xc = E.cross_control(s, day, RULE, board, sips, sm)
                for n in GRID["top_n"]:
                    if rank < n:
                        a = acc[(n, dip)]
                        a[0] += 1; a[1] += g - gc; a[2] += g
                        if xc is not None:
                            a[3] += 1; a[4] += g - xc
        rng = _seeded(f"{day}|{s}", "trend-rnd")
        key = "top" if rank is not None else "cmp"
        for k in rng.sample(range(WATCH_START, WATCH_END + 1), 5):
            g = E.markout(sip, k)
            if g is not None:
                info["rnd_" + key][0] += 1; info["rnd_" + key][1] += g
        a0, b0 = sip.get(WATCH_START), sip.get(15 * 60 + 59)
        if a0 and b0 and a0[0]:
            info["hold_" + key][0] += 1; info["hold_" + key][1] += b0[1] / a0[0] - 1
    acc["_info"] = info
    return day, acc


def summarize(per_day: dict, days: list[str], cut: str) -> dict:
    out = {}
    for c in CELLS:
        rec = {}
        for h, ds in (("A", [d for d in days if d <= cut]), ("B", [d for d in days if d > cut])):
            lifts = [per_day[d][c][1] / per_day[d][c][0] for d in ds if per_day[d][c][0]]
            gross = [per_day[d][c][2] / per_day[d][c][0] for d in ds if per_day[d][c][0]]
            cross = [per_day[d][c][4] / per_day[d][c][3] for d in ds if per_day[d][c][3]]
            mu, t, nd = T._de(lifts)
            gm = T._de(gross)[0]
            rec[h] = {"lift": mu, "t": t, "days": nd, "n": sum(per_day[d][c][0] for d in ds),
                      "net10": None if gm is None else gm - E.COST, "net3": None if gm is None else gm - 0.0003,
                      "cross": T._de(cross)[0]}
        out[c] = rec
    return out


MIN_A_SIGNALS, MIN_A_DAYS = 150, 30


def report(summary: dict, per_day: dict, days: list[str], cut: str) -> str:
    bp = lambda x: "-" if x is None else f"{x * 1e4:+.1f}"  # noqa: E731
    tt = lambda x: "-" if x is None else f"{x:+.2f}"  # noqa: E731
    ok = [c for c in CELLS if summary[c]["A"]["n"] >= MIN_A_SIGNALS and summary[c]["A"]["days"] >= MIN_A_DAYS
          and summary[c]["A"]["lift"] is not None]
    L = ["# Trending names, buy the %R dips (trend_dip_prereg)", "",
         f"{len(CELLS)} cells (top N x dip); {len(days)} sessions; pick on half A (<= {cut}), confirm on held-out B.", ""]
    if not ok:
        L.append("No cell met the discovery minimums: UNDERPOWERED.")
    else:
        best = max(ok, key=lambda c: (summary[c]["A"]["lift"], c))
        b = summary[best]["B"]
        L += [f"**Picked on A:** top {best[0]} names, dip {best[1]} (A lift {bp(summary[best]['A']['lift'])} bp, t "
              f"{tt(summary[best]['A']['t'])}, n {summary[best]['A']['n']})",
              f"**Held-out B:** lift {bp(b['lift'])} bp (t {tt(b['t'])}), n {b['n']}, {b['days']} days, net@10 "
              f"{bp(b['net10'])}, net@3 {bp(b['net3'])}, cross {bp(b['cross'])} -> **{T.verdict(b)}**", ""]
    L += ["| cell (N, dip) | A lift (t, n) | B lift (t, n) | B cross | B verdict |", "|---|---:|---:|---:|---|"]
    for c in CELLS:
        a, bb = summary[c]["A"], summary[c]["B"]
        L.append(f"| {c} | {bp(a['lift'])} ({tt(a['t'])}, {a['n']}) | {bp(bb['lift'])} ({tt(bb['t'])}, {bb['n']}) | "
                 f"{bp(bb['cross'])} | {T.verdict(bb)} |")

    def pooled(key):
        vals = [per_day[d]["_info"][key][1] / per_day[d]["_info"][key][0] for d in days
                if per_day[d]["_info"][key][0]]
        return T._de(vals)
    L += ["", "## Information only", ""]
    for dip in GRID["dip"]:
        vals = [per_day[d]["_info"]["cmp_dip"][dip][1] / per_day[d]["_info"]["cmp_dip"][dip][0] for d in days
                if per_day[d]["_info"]["cmp_dip"][dip][0]]
        mu, t, nd = T._de(vals)
        L.append(f"- the same dip rule (dip {dip}) on the 100-name comparison sample: lift {bp(mu)} bp (t {tt(t)}, {nd} days)")
    for key, label in (("rnd_top", "random 10-min holds in the trending names"),
                       ("rnd_cmp", "random 10-min holds in the comparison names"),
                       ("hold_top", "10:31 open -> close hold, trending names"),
                       ("hold_cmp", "10:31 open -> close hold, comparison names")):
        mu, t, nd = pooled(key)
        L.append(f"- {label}: {bp(mu)} bp gross (t {tt(t)}, {nd} days)")
    ntop = [per_day[d]["_info"]["n_top"] for d in days]
    L.append(f"- trending names selected per session: median {st.median(ntop) if ntop else 0}, "
             f"sessions with none {sum(1 for x in ntop if x == 0)}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    lz = [d for d in P.usable_days() if d["day"] <= LAST_DAY]
    days = [d["day"] for d in lz]
    cut = E.split(days)
    print(f"sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; A ends {cut}; "
          f"{len(CELLS)} cells", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    if not (len(days) == N_DAYS and days[0] == FIRST_DAY and cut == CUT):
        print("REFUSING: window differs from the prereg")
        return
    per_day = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (day, acc) in enumerate(ex.map(day_job, [(d["day"], d._prev) for d in lz])):
            per_day[day] = acc
            if k % 10 == 0:
                print(f"  {day} ({k + 1}/{len(lz)})", flush=True)
    md = report(summarize(per_day, days, cut), per_day, days, cut)
    print(md)
    with open(os.path.join(ROOT, "ai_reports", "warehouse", "trend_dip_result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
