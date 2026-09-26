#!/usr/bin/env python3
"""edge_c_grid.py — Round-1 strategy-edge study, parts B and C (2026-09-26).

Pre-registered split: Stage B window 2026-06-22..2026-09-15 (60 trading days);
first 40 days in-sample (IS), last 20 out-of-sample (OOS). Nothing is fitted;
IS/OOS is a stability check across cells.

Universes
  CUR  Claude's Stage B universe per day: the most-traded $20-$100 names by the
       prior day's dollar volume (combined_stage_b_rows.pkl), ~259/day.
  LIQ  fixed pre-registered list of ~100 large caps/ETFs (edge_fetch.LIQUID);
       'LIQ<=3' = those whose PRIOR day's SIP median spread was <= 3 bp.
Entries (decision at the close of an IEX 1m bar, price = that close)
  random  every 5 minutes 09:35..15:30 for every name (the random-entry
          baseline: all eligible minutes), needs a fresh IEX print (<=5 min).
  wr      the live %R trigger on IEX 1m bars from 04:00: fast %R(21,EWM7)
          crosses up through -50 while slow %R(112,EWM3) is above its value
          2 bars earlier; one per name per 15 min.
Holds 5/15/30/60/120 min and to 15:50; entry buckets by decision time
  b0 09:35-10:30, b1 10:30-12:00, b2 12:00-14:00, b3 14:00-15:30.
Cost: full SIP spread at entry (Claude's model; see edge_common.SpreadModel).
Passive bound: mid entry pays spread/2, bid entry pays 0. Fill proxy: an IEX
1m low strictly below the limit within 5 minutes.
"""
from __future__ import annotations

import os
import pickle
import sys
import time
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import edge_common as ec  # noqa: E402
from edge_fetch import LIQUID  # noqa: E402
import mid_rise_runway_study as mr  # noqa: E402

HS = (5, 15, 30, 60, 120, "eod")
BK = ((5, 60), (60, 150), (150, 270), (270, 361))
BKN = ("09:35-10:30", "10:30-12:00", "12:00-14:00", "14:00-15:30")
REC = os.path.join(ROOT, "ai_reports", "edge_c_records.npz")
COLS = ["day", "uni", "kind", "bk", "tod", "px", "sdid", "symi",
        "g5", "g15", "g30", "g60", "g120", "geod", "dip5"]


def bucket(T):
    for b, (a, z) in enumerate(BK):
        if a <= T < z:
            return b
    return -1


def entry_row(D, e):
    """Gross returns (bp) for an entry at bar e (decision T=e+1), and dip5 = the
    lowest IEX 1m low in the next 5 minutes vs the entry price (bp), for the
    passive-fill proxy (computed at report time once the spread is known)."""
    p0 = D.C[e]
    out = []
    for H in HS:
        j = 379 if H == "eod" else e + H
        out.append((D.Cf[j] / p0 - 1) * 1e4 if j <= 379 and j > e else np.nan)
    lows = D.L[e + 1:min(390, e + 6)]
    lmin = np.nanmin(lows) if np.any(~np.isnan(lows)) else np.nan
    return out + [(lmin / p0 - 1) * 1e4 if not np.isnan(lmin) else np.nan]


def wr_signals(D, lo_px, hi_px):
    t, o, h, l, c, v = D.raw
    if len(c) < 60:
        return []
    fast = mr.percent_r_series(list(h), list(l), list(c), 21, 7.0)
    slow = mr.percent_r_series(list(h), list(l), list(c), 112, 3.0)
    out, last = [], -10 ** 9
    for i in range(2, len(c)):
        e = int(D.k_raw[i])
        if not (4 <= e <= 359) or not (lo_px <= c[i] <= hi_px):
            continue
        f0, f1 = fast[i - 1], fast[i]
        if f0 is None or f1 is None or slow[i] is None or slow[i - 2] is None:
            continue
        if f0 <= -50 < f1 and slow[i] > slow[i - 2] and e - last >= 15:
            out.append(e)
            last = e
    return out


def build():
    days, cur, _rows = ec.load_days()
    if os.environ.get("EDGE_MAXDAYS"):
        days = days[:int(os.environ["EDGE_MAXDAYS"])]
    allsyms = sorted(set(s_ for d in days for s_ in cur[d]) | set(LIQUID))
    symix = {s_: i for i, s_ in enumerate(allsyms)}
    recs = []
    sdid = 0
    prevday = None
    prevbars = {}
    allfiles = sorted(f[:-4] for f in os.listdir(ec.BARDIR) if f.endswith(".pkl"))
    pf = [d for d in allfiles if d < days[0]]
    if pf:
        prevbars = ec.load_bars(pf[-1]) or {}
    for di, day in enumerate(days):
        t0 = time.time()
        bars = ec.load_bars(day)
        if bars is None:
            print(f"  {day}: no bars yet"); continue
        for uni, syms in ((0, cur[day]), (1, LIQUID)):
            for sym in syms:
                B = bars.get(sym)
                if B is None or len(B[0]) < 30:
                    continue
                PB = prevbars.get(sym)
                if PB is not None and len(PB[0]):
                    # prior session's bars ahead of today's, so the slow %R(112) is warm at the open
                    B = tuple(np.concatenate([PB[q], B[q]]) for q in range(6))
                D = ec.Dense(B, day)
                if D.nbars < 60:
                    continue
                sdid += 1
                ents = [(0, e) for e in range(4, 360, 5) if not np.isnan(D.C[e]) or D.age[e] <= 5]
                ents = [(k, e) for k, e in ents if D.age[e] <= 5]
                lo_px, hi_px = (20, 100) if uni == 0 else (20, 1e9)
                ents += [(1, e) for e in wr_signals(D, lo_px, hi_px)]
                for kind, e in ents:
                    p0 = D.Cf[e]
                    if np.isnan(p0) or p0 < 20 or (uni == 0 and p0 > 100):
                        continue
                    if kind == 0:
                        D.C[e] = p0 if np.isnan(D.C[e]) else D.C[e]
                    T = e + 1
                    recs.append([di, uni, kind, bucket(T), T, p0, sdid, symix[sym]] + entry_row(D, e))
        prevday = day
        prevbars = bars
        print(f"  {day}: {len(recs)} records so far ({time.time() - t0:.0f}s)", flush=True)
    A = np.array(recs, dtype=np.float64)
    np.savez_compressed(REC if not os.environ.get("EDGE_MAXDAYS") else "/tmp/edge_c_smoke.npz", A=A, cols=np.array(COLS),
                        days=np.array(days), syms=np.array(allsyms))
    return A, days, allsyms


def add_costs(A, days, syms):
    """Append spr (bp, Claude's model at entry), liq3 (prior-day SIP median spread <= 3 bp),
    and the passive-entry fields."""
    sm = ec.SpreadModel()
    print(f"spread model: {sm.n_obs} SIP samples, {len(sm.sd)} sym-days; ToD profile "
          + " ".join(f"{k}:{v:.2f}" for k, v in sorted(sm.prof.items())), flush=True)
    di, si, tod, px = A[:, 0].astype(int), A[:, 7].astype(int), A[:, 4], A[:, 5]
    cache = {}
    spr = np.empty(len(A)); liq = np.zeros(len(A))
    for k in range(len(A)):
        key = (si[k], di[k], ec.slot_of(tod[k]))
        v = cache.get(key)
        if v is None:
            v = sm.get(syms[si[k]], days[di[k]], tod[k], px[k])
            cache[key] = v
        spr[k] = v
        if A[k, 1] == 1:
            pd_ = days[di[k] - 1] if di[k] > 0 else days[0]
            liq[k] = 1.0 if sm.sd.get((syms[si[k]], pd_), sm.sd.get((syms[si[k]], days[di[k]]), 99)) <= 3.0 else 0.0
    dip = A[:, COLS.index("dip5")]
    fm = (dip < 0).astype(float)
    fb = (dip < -spr / 2).astype(float)
    extra = [spr, liq, fm, fb]
    names = ["spr", "liq3", "fm5", "fb5"]
    for H in (30, 60, "eod"):
        g = A[:, COLS.index(f"g{H}")]
        extra += [g, ((1 + g / 1e4) / (1 - spr / 2e4) - 1) * 1e4]
        names += [f"gm{H}", f"gb{H}"]
    COLS.extend(names)
    return np.column_stack([A] + extra)


def col(A, name):
    return A[:, COLS.index(name)]


def report(A, days):
    D_ = col(A, "day"); IS = D_ < ec.N_IS
    uni = col(A, "uni"); kind = col(A, "kind"); bk = col(A, "bk"); spr = col(A, "spr")
    liq3 = col(A, "liq3"); px = col(A, "px")
    n_is, n_oos = min(ec.N_IS, len(days)), max(0, len(days) - ec.N_IS)
    print(f"\n{len(A)} records; days {days[0]}..{days[-1]}; IS = first {n_is} days, OOS = last {n_oos}")
    print(f"median spread bp: CUR {np.median(spr[uni == 0]):.1f}, LIQ {np.median(spr[uni == 1]):.1f}, "
          f"LIQ<=3 {np.median(spr[(uni == 1) & (liq3 == 1)]):.1f}")
    unis = (("CUR", uni == 0), ("LIQ<=3", (uni == 1) & (liq3 == 1)))
    out = []

    def cell(mask, g, cost):
        ok = mask & ~np.isnan(g)
        r = {}
        for part, pm, nd in (("IS", IS, n_is), ("OOS", ~IS, n_oos)):
            m = ok & pm
            r[part] = ec.stats(g[m] - cost[m], D_[m], gross=g[m], n_days_total=nd) if m.sum() >= 20 else None
        return r

    print("\n== C. Holding-period grid: net bp/trade (full spread at entry), IS | OOS; "
          "WR also vs matched random (same sym-day-bucket) ==")
    sdid = col(A, "sdid")
    for uname, um in unis:
        for H in HS:
            g = col(A, f"g{H}")
            for b in list(range(4)) + [None]:
                bm = (bk == b) if b is not None else (bk >= 0)
                rr = cell(um & bm & (kind == 0), g, spr)
                rw = cell(um & bm & (kind == 1), g, spr)
                # matched: WR net minus mean random net in the same sym-day-bucket
                diff = {}
                for part, pm in (("IS", IS), ("OOS", ~IS)):
                    base = defaultdict(list)
                    mm = um & bm & (kind == 0) & pm & ~np.isnan(g)
                    for k_, b_, x in zip(sdid[mm], bk[mm], g[mm] - spr[mm]):
                        base[(k_, b_)].append(x)
                    base = {k: np.mean(v) for k, v in base.items()}
                    mw = um & bm & (kind == 1) & pm & ~np.isnan(g)
                    ds = [x - base[(k_, b_)] for k_, b_, x in zip(sdid[mw], bk[mw], g[mw] - spr[mw]) if (k_, b_) in base]
                    dd = [d for d, k_, b_ in zip(D_[mw], sdid[mw], bk[mw]) if (k_, b_) in base]
                    diff[part] = ec.stats(ds, dd) if len(ds) >= 20 else None
                lab = f"{uname:6} H={str(H):>4} {BKN[b] if b is not None else 'all':>11}"
                out.append({"uni": uname, "H": H, "bk": b, "rand": rr, "wr": rw, "wr_vs_matched": diff})
                def s2(r):
                    a, z = r.get("IS"), r.get("OOS")
                    f = lambda s: f"{s['net']:+6.1f}(t{s['t']:+4.1f})" if s else "     -       "
                    return f"{f(a)} | {f(z)}"
                pr = lambda r, k: (r.get(k) or {}).get("per_day", 0)
                flag = ""
                for nm, r in (("rand", rr), ("wr", rw)):
                    if r.get("IS") and r.get("OOS") and r["IS"]["net"] > 0 and r["OOS"]["net"] > 0:
                        flag += f" <<{nm} net>0 both"
                print(f"  {lab}  rand {s2(rr)}   wr {s2(rw)} ({pr(rw,'OOS'):4.1f}/d)   wr-matched {s2(diff)}{flag}")
    # full detail for the 'all buckets' rows
    print("\n== C detail (all entry buckets) ==")
    for uname, um in unis:
        for kn, kv in (("random", 0), ("wr", 1)):
            for H in HS:
                g = col(A, f"g{H}")
                r = cell(um & (kind == kv) & (bk >= 0), g, spr)
                print(f"  {uname:6} {kn:6} H={str(H):>4} IS  {ec.fmt(r['IS'])}")
                print(f"  {'':6} {'':6} {'':6} OOS {ec.fmt(r['OOS'])}")

    print("\n== B1. Universe / cost levers (random entries, all buckets): market | mid-entry bound | bid-entry bound ==")
    levers = (("CUR all", uni == 0), ("CUR spr<=10", (uni == 0) & (spr <= 10)), ("CUR spr<=5", (uni == 0) & (spr <= 5)),
              ("LIQ all", uni == 1), ("LIQ<=3", (uni == 1) & (liq3 == 1)),
              ("SPY+QQQ", np.zeros(len(A), bool)))
    # SPY/QQQ mask via price? use sdid lookup not available: rebuild from records is costly; skip exact tag
    for lname, lm in levers[:-1]:
        for H in (30, 60, "eod"):
            g = col(A, f"g{H}")
            for part, pm, nd in (("IS", IS, n_is), ("OOS", ~IS, n_oos)):
                m = lm & (kind == 0) & pm & ~np.isnan(g)
                if m.sum() < 20:
                    continue
                a = ec.stats(g[m] - spr[m], D_[m], gross=g[m], n_days_total=nd)
                b_ = ec.stats(g[m] - spr[m] / 2, D_[m], gross=g[m], n_days_total=nd)
                c_ = ec.stats(g[m], D_[m], gross=g[m], n_days_total=nd)
                print(f"  {lname:12} H={str(H):>4} {part:3} n {a['n']:7d} gross {a['gross']:+6.1f} spr {np.mean(spr[m]):5.1f}  "
                      f"mkt {a['net']:+6.1f} [{a['lo']:+5.1f},{a['hi']:+5.1f}]  mid {b_['net']:+6.1f}  bid {c_['net']:+6.1f} [{c_['lo']:+5.1f},{c_['hi']:+5.1f}]")
    print("\n== B2. Passive entry with a fill proxy (IEX low strictly through the limit within 5 min; exit at market) ==")
    fm, fb = col(A, "fm5"), col(A, "fb5")
    for lname, lm in (("CUR", uni == 0), ("LIQ<=3", (uni == 1) & (liq3 == 1))):
        for kn, kv in (("random", 0), ("wr", 1)):
            for H in (30, 60, "eod"):
                gm, gb, g = col(A, f"gm{H}"), col(A, f"gb{H}"), col(A, f"g{H}")
                for part, pm, nd in (("IS", IS, n_is), ("OOS", ~IS, n_oos)):
                    m = lm & (kind == kv) & pm & ~np.isnan(g)
                    if m.sum() < 20:
                        continue
                    mk = ec.stats(g[m] - spr[m], D_[m], gross=g[m], n_days_total=nd)
                    mm = m & (fm == 1); mb = m & (fb == 1)
                    sm_ = ec.stats(gm[mm] - spr[mm] / 2, D_[mm], gross=gm[mm], n_days_total=nd) if mm.sum() > 20 else None
                    sb_ = ec.stats(gb[mb] - spr[mb] / 2, D_[mb], gross=gb[mb], n_days_total=nd) if mb.sum() > 20 else None
                    att_m = np.where(fm[m] == 1, gm[m] - spr[m] / 2, 0.0).mean()
                    att_b = np.where(fb[m] == 1, gb[m] - spr[m] / 2, 0.0).mean()
                    f = lambda s: f"{s['net']:+6.1f} [{s['lo']:+5.1f},{s['hi']:+5.1f}] (gross {s['gross']:+6.1f})" if s else "-"
                    print(f"  {lname:6} {kn:6} H={str(H):>4} {part:3} market {mk['net']:+6.1f} (gross {mk['gross']:+6.1f}) | "
                          f"mid fill {fm[m].mean():5.1%} per-fill {f(sm_)} per-attempt {att_m:+5.1f} | "
                          f"bid fill {fb[m].mean():5.1%} per-fill {f(sb_)} per-attempt {att_b:+5.1f}")
    pickle.dump(out, open("/tmp/edge_c_cells.pkl", "wb"))


def validate(days):
    """IEX forward 30m vs Claude's SIP fwd30 on the same (sym, moment)."""
    _d, _c, rows = ec.load_days()
    xs, ys = [], []
    for day in days[::6]:
        bars = ec.load_bars(day)
        if not bars:
            continue
        ot = ec.open_ts(day)
        cache = {}
        for r in rows[day]:
            B = bars.get(r["sym"])
            if B is None:
                continue
            if r["sym"] not in cache:
                cache[r["sym"]] = ec.Dense(B, day)
            D = cache[r["sym"]]
            e = int((r["t"] - ot) // 60) - 1
            if 0 <= e and e + 30 <= 389 and D.age[e] <= 5:
                xs.append((D.Cf[e + 30] / D.Cf[e] - 1) * 1e4); ys.append(r["fwd30"])
    xs, ys = np.array(xs), np.array(ys)
    print(f"validation vs Claude SIP fwd30 ({len(xs)} moments, every 6th day): mean IEX {xs.mean():+.1f} vs SIP {ys.mean():+.1f} bp, "
          f"corr {np.corrcoef(xs, ys)[0, 1]:.3f}, median |diff| {np.median(np.abs(xs - ys)):.1f} bp")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        z = np.load(REC)
        A, days, syms = z["A"], [str(d) for d in z["days"]], [str(x) for x in z["syms"]]
    else:
        A, days, syms = build()
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        sys.exit(0)
    A = add_costs(A, days, syms)
    validate(days)
    report(A, days)
