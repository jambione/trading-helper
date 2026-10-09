#!/usr/bin/env python3
"""needle_cells_oos.py — pre-registered in docs/studies/needle_cells_oos_prereg.json (3168fc7). Nothing here may change it.

Out-of-period test of the needle study's name cells on the cached SIP-breakout universe
(ai_reports/allsym/sipbrk_min_*.pkl: 40 largest >= 2% gappers a day, 2026-05-01..09-11, RTH bars (ts, o, h, c, v)).

  C2 (verdict)  pm_range in [2.433, 3.481) % AND day_chg >= 4.718 %, any sampled minute
  C1 (info)     day_chg in [1.583, 4.718) % AND bar start 10:55..11:55
  C2_DAY (info) day_chg >= 4.718 % alone

Moments: bar starts 09:45..15:10 ET on the 5-minute grid, close in [$20, $100]. Enter at the open of the next bar
(within 5 min); exit at the close of the last bar starting <= min(entry + 30 min, 15:55); dropped when that leaves a
hole (no bar after the target within 45 min of entry). net30 = gross - 10 bp.
Control: per day and ET hour, non-cell moments of OTHER name-days. Averaging: moment minus its own day-hour control
-> mean per name-day -> mean per day. Halves = alternate trading days. t clustered by day.

USAGE (on the mini, AFTER THE CLOSE: the fetch shares the live engine's Alpaca keys)
  .venv/bin/python tools/studies/needle_cells_oos.py fetch      # premarket 04:00-09:29 + SPY RTH per day, cached
  .venv/bin/python tools/studies/needle_cells_oos.py score      # report -> ai_reports/needle_oos/report.md
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bars  # noqa: E402

ET = bars.ET
ALLSYM = os.path.join(ROOT, "ai_reports", "allsym")
DAILY = os.path.join(ALLSYM, "daily_2025-09-01_2026-10-02.pkl")
OUT = os.path.join(ROOT, "ai_reports", "needle_oos")
LO, HI = "2026-05-01", "2026-09-11"

DAY_CHG_LO, DAY_CHG_HI = 1.583, 4.718
PM_LO, PM_HI = 2.433, 3.481
C1_START, C1_END = 10 * 60 + 55, 11 * 60 + 55           # bar-start minutes, inclusive (needle mins [86, 151))
GRID_LO, GRID_HI = 9 * 60 + 45, 15 * 60 + 10
BAND = (20.0, 100.0)
COST_BP, COST_INFO_BP = 10.0, 4.0
HOLD, HOLE, CAP_MIN = 30 * 60, 45 * 60, 15 * 60 + 55


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def day_ts(day, minute):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return (d + timedelta(minutes=minute)).timestamp()


# ---------------------------------------------------------------- cells (pure)

def in_c2(day_chg, pm_range):
    return pm_range is not None and PM_LO <= pm_range < PM_HI and day_chg >= DAY_CHG_HI


def in_c1(day_chg, m):
    return DAY_CHG_LO <= day_chg < DAY_CHG_HI and C1_START <= m <= C1_END


def in_c2_day(day_chg, pm_range):
    return day_chg >= DAY_CHG_HI


def pm_range_of(pm_bars):
    """pm_bars: [(ts, high, low)] 04:00-09:29. >= 1 bar is enough (as the needle)."""
    if not pm_bars:
        return None
    lo = min(b[2] for b in pm_bars)
    return (max(b[1] for b in pm_bars) / lo - 1) * 100 if lo > 0 else None


def outcome(rows, i, day):
    """Decision at the close of rows[i] (ts, o, h, c, v). Returns (entry_ts, exit_ts, gross_bp) or None."""
    if i + 1 >= len(rows) or rows[i + 1][0] - rows[i][0] > 5 * 60:
        return None
    te, pe = rows[i + 1][0], rows[i + 1][1]
    if pe <= 0:
        return None
    cap = day_ts(day, CAP_MIN)
    tgt = min(te + HOLD, cap)
    k = i + 1
    while k + 1 < len(rows) and rows[k + 1][0] <= tgt:
        k += 1
    if rows[k][0] < tgt and tgt < cap:                    # target fell in a gap: need a bar after it within 45 min
        if not (k + 1 < len(rows) and rows[k + 1][0] <= te + HOLE):
            return None
    return te, rows[k][0], (rows[k][3] / pe - 1) * 1e4


# ---------------------------------------------------------------- statistic (pure)

def tstat(xs):
    n = len(xs)
    if n < 2:
        return (statistics.fmean(xs) if xs else float("nan")), float("nan")
    m, sd = statistics.fmean(xs), statistics.stdev(xs)
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan"))


def score_cell(moments, cell, cost_bp, days_subset=None, drop_nd=()):
    """moments: dicts with day, nd (name-day key), hour, net-free gross, and cell flags.
    Returns per-day lists of (net30) and (net30 - control) under the prereg's averaging order, plus name-day means."""
    ctl_sum = defaultdict(lambda: defaultdict(float))   # (day, hour) -> nd -> sum of non-cell net
    ctl_n = defaultdict(lambda: defaultdict(int))
    for x in moments:
        if not x[cell]:
            k = (x["day"], x["hour"])
            ctl_sum[k][x["nd"]] += x["gross"] - cost_bp
            ctl_n[k][x["nd"]] += 1
    tot = {k: (sum(v.values()), sum(ctl_n[k].values())) for k, v in ctl_sum.items()}
    nd_net, nd_diff = defaultdict(list), defaultdict(list)
    for x in moments:
        if not x[cell] or x["nd"] in drop_nd or (days_subset is not None and x["day"] not in days_subset):
            continue
        k = (x["day"], x["hour"])
        s, n = tot.get(k, (0.0, 0))
        s -= ctl_sum[k].get(x["nd"], 0.0)
        n -= ctl_n[k].get(x["nd"], 0)
        net = x["gross"] - cost_bp
        nd_net[x["nd"]].append(net)
        if n > 0:
            nd_diff[x["nd"]].append(net - s / n)
    by_day_net, by_day_diff = defaultdict(list), defaultdict(list)
    nd_mean_diff = {}
    for nd, v in nd_net.items():
        by_day_net[nd[1]].append(statistics.fmean(v))
    for nd, v in nd_diff.items():
        nd_mean_diff[nd] = statistics.fmean(v)
        by_day_diff[nd[1]].append(nd_mean_diff[nd])
    day_net = {d: statistics.fmean(v) for d, v in by_day_net.items()}
    day_diff = {d: statistics.fmean(v) for d, v in by_day_diff.items()}
    return {"n_nd": len(nd_net), "n_days": len(day_net), "n_mom": sum(len(v) for v in nd_net.values()),
            "day_net": day_net, "day_diff": day_diff, "nd_mean_diff": nd_mean_diff}


def verdict(moments, halves, cost_bp=COST_BP, cell="c2"):
    lines, ok = [], True
    for h in ("A", "B"):
        r = score_cell(moments, cell, cost_bp, days_subset=halves[h])
        mn, tn = tstat(list(r["day_net"].values()))
        md, td = tstat(list(r["day_diff"].values()))
        enough = r["n_nd"] >= 30 and r["n_days"] >= 20
        good = enough and mn > 0 and md >= 5
        ok &= good
        lines.append(f"half {h}: name-days {r['n_nd']}, days {r['n_days']}, moments {r['n_mom']} | net30 {mn:+.1f} bp "
                     f"(t {tn:+.2f}) | minus control {md:+.1f} bp (t {td:+.2f}) | "
                     f"{'ok' if good else ('UNDERPOWERED' if not enough else 'fails')}")
    r = score_cell(moments, cell, cost_bp)
    md, td = tstat(list(r["day_diff"].values()))
    top3 = sorted(r["nd_mean_diff"], key=r["nd_mean_diff"].get, reverse=True)[:3]
    r3 = score_cell(moments, cell, cost_bp, drop_nd=set(top3))
    md3, td3 = tstat(list(r3["day_diff"].values()))
    ok &= td >= 2.0 and md3 >= 5 and td3 >= 2.0
    lines.append(f"pooled minus control {md:+.1f} bp (t {td:+.2f}, {len(r['day_diff'])} days); without top 3 name-days "
                 f"{', '.join(f'{s} {d}' for s, d in top3)}: {md3:+.1f} bp (t {td3:+.2f})")
    return ok, lines


# ---------------------------------------------------------------- data

def cached_days():
    out = []
    for p in sorted(glob.glob(os.path.join(ALLSYM, "sipbrk_min_*.pkl"))):
        d = os.path.basename(p)[len("sipbrk_min_"):-4]
        if LO <= d <= HI:
            out.append(d)
    return out


def pm_path(day):
    return os.path.join(OUT, f"pm_{day}.json")


def fetch(force=False):
    now = datetime.now(ET)
    if not force and now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute < 16 * 60 + 20:
        sys.exit("refusing to fetch during the session (shared Alpaca keys); run after 16:20 ET or pass --force")
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    if cl is None:
        sys.exit("no Alpaca data client")
    os.makedirs(OUT, exist_ok=True)
    days = cached_days()
    for n, day in enumerate(days):
        if os.path.exists(pm_path(day)) and json.load(open(pm_path(day))).get("status") == "ok":
            continue
        syms = sorted(pickle.load(open(os.path.join(ALLSYM, f"sipbrk_min_{day}.pkl"), "rb")))
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        rec = {"day": day, "status": "failed", "syms": syms, "pm": {}, "spy": [], "errors": []}
        for attempt in range(5):
            try:
                r = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4), end=d.replace(hour=9, minute=30),
                    feed=DataFeed.SIP, adjustment=Adjustment.SPLIT))
                pm = {s: [(b.timestamp.timestamp(), float(b.high), float(b.low)) for b in rows]
                      for s, rows in (r.data or {}).items()}
                time.sleep(1.0)
                q = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=9, minute=30), end=d.replace(hour=16), feed=DataFeed.SIP))
                spy = [(b.timestamp.timestamp(), float(b.open), float(b.close)) for b in (q.data or {}).get("SPY", [])]
                if not spy:
                    raise RuntimeError("empty SPY response")
                rec.update(status="ok", pm={s: pm.get(s, []) for s in syms}, spy=spy)
                break
            except Exception as e:  # noqa: BLE001
                rec["errors"].append(str(e)[:120])
                time.sleep(10 * (attempt + 1))
        json.dump(rec, open(pm_path(day), "w"))
        nopm = sum(1 for s in syms if not rec["pm"].get(s))
        print(f"{n + 1}/{len(days)} {day} {rec['status']} names {len(syms)} no-pm {nopm}"
              + (f" errors {rec['errors'][-1]}" if rec["status"] != "ok" else ""), flush=True)
        time.sleep(1.0)


def prior_closes(daily):
    out = {}
    for sym, rows in daily.items():
        out[sym] = ([r[0] for r in rows], [r[4] for r in rows])
    return out


def build_moments():
    daily = pickle.load(open(DAILY, "rb"))
    pc = prior_closes(daily)
    days = cached_days()
    moments, drops = [], defaultdict(int)
    fetch_failed = 0
    spy_by_day = {}
    for day in days:
        mb = pickle.load(open(os.path.join(ALLSYM, f"sipbrk_min_{day}.pkl"), "rb"))
        pm = json.load(open(pm_path(day))) if os.path.exists(pm_path(day)) else {"status": "missing"}
        if pm.get("status") != "ok":
            fetch_failed += len(mb)
            pmd = {}
        else:
            pmd = pm["pm"]
            spy_by_day[day] = {round(t): (o, c) for t, o, c in pm["spy"]}
        for sym, rows in mb.items():
            nd = (sym, day)
            ds, cs = pc.get(sym, ([], []))
            j = bisect.bisect_left(ds, day) - 1
            if j < 0:
                drops["no_prior_close"] += 1
                continue
            prev = cs[j]
            pmb = pmd.get(sym) if pm.get("status") == "ok" else None
            if pm.get("status") == "ok" and not pmb:
                drops["no_premarket_bars (name-days)"] += 1
            pmr = pm_range_of(pmb) if pmb else None
            for i, r in enumerate(rows):
                m = et_min(r[0])
                if not (GRID_LO <= m <= GRID_HI) or (m - GRID_LO) % 5:
                    continue
                px = r[3]
                band = BAND[0] <= px <= BAND[1]
                dc = (px / prev - 1) * 100
                cells = {"c2": in_c2(dc, pmr), "c1": in_c1(dc, m), "c2_day": in_c2_day(dc, pmr),
                         "c2_pm5": in_c2(dc, pmr) and pmb is not None and len(pmb) >= 5}
                o = outcome(rows, i, day)
                if o is None:
                    drops["no_exit_bar " + ("cell" if cells["c2"] else "control")] += 1
                    continue
                te, tx, g = o
                moments.append({"day": day, "nd": nd, "hour": m // 60, "gross": g, "band": band, "te": te, "tx": tx,
                                **cells})
    return days, moments, dict(drops), fetch_failed, spy_by_day


def spy_beta(moments, spy_by_day, cell):
    xs, ys = [], []
    for x in moments:
        if not x[cell]:
            continue
        s = spy_by_day.get(x["day"], {})
        a, b = s.get(round(x["te"])), s.get(round(x["tx"]))
        if a and b and a[0] > 0:
            xs.append((b[1] / a[0] - 1) * 1e4)
            ys.append(x["gross"])
    if len(xs) < 10 or statistics.pvariance(xs) == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    beta = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs)
    return beta, len(xs), mx


def score():
    os.makedirs(OUT, exist_ok=True)
    days, moments, drops, fetch_failed, spy_by_day = build_moments()
    n_nd = len({x["nd"] for x in moments}) or 1
    halves = {"A": set(days[0::2]), "B": set(days[1::2])}
    L = ["# Needle cells out of period (prereg docs/studies/needle_cells_oos_prereg.json)", "",
         f"days {len(days)} ({days[0]}..{days[-1]}), moments {len(moments)}, name-days {n_nd}; drops {drops}; "
         f"name-days with failed premarket fetch {fetch_failed}", ""]
    total_nd = sum(len(pickle.load(open(os.path.join(ALLSYM, f"sipbrk_min_{d}.pkl"), "rb"))) for d in days)
    if fetch_failed > 0.05 * total_nd:
        L.append(f"**FAILED-DATA**: premarket fetch failed for {fetch_failed} of {total_nd} name-days (> 5%)")
        open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
        print("\n".join(L))
        return
    banded = [x for x in moments if x["band"]]
    ok, lines = verdict(banded, halves)
    L += ["## C2 (verdict cell), $20-100, 10 bp", *[f"- {s}" for s in lines], "",
          f"**PRIMARY: {'PASS' if ok else 'FAIL'}** (both halves: >= 30 name-days, >= 20 days, net30 > 0, minus "
          f"control >= +5 bp; pooled t >= 2.0; still >= +5 bp with t >= 2.0 without the top 3 name-days)", "",
          "## Information only (no verdict)"]
    for lab, mm, cell, cost in (("C1 (demoted)", banded, "c1", COST_BP), ("C2 day_chg leg alone", banded, "c2_day", COST_BP),
                                ("C2 with >= 5 premarket bars", banded, "c2_pm5", COST_BP), ("C2 at 4 bp", banded, "c2", COST_INFO_BP),
                                ("C2 $10+ (no band)", moments, "c2", COST_BP)):
        _, ls = verdict(mm, halves, cost, cell)
        L.append(f"- **{lab}**")
        L += [f"    - {s}" for s in ls]
    for cell in ("c2", "c2_day"):
        b = spy_beta(banded, spy_by_day, cell)
        L.append(f"- SPY beta of {cell} moments: " + ("n/a" if b is None else f"{b[0]:.2f} (n {b[1]}, mean SPY move {b[2]:+.1f} bp)"))
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch(force="--force" in sys.argv)
    elif cmd == "score":
        score()
    else:
        sys.exit(__doc__)
