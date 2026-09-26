#!/usr/bin/env python3
"""edge5_t2.py — Round-5 T2 earnings drift (pre-registered in edge5b_prereg.json).
USAGE  edge5_t2.py fetch   -> Finnhub free /stock/earnings (last 4 quarters/symbol; key via config loader, never printed), 1 req/1.2 s
       edge5_t2.py         -> study
Finnhub free tier gives only the last 4 quarters and no report dates (calendar history returns empty), so earnings
reaction days are INFERRED from daily bars: relative volume spike (stock vol/20d median over SPY's) >= 2.5, greedy by
size with >= 40 sessions between events per symbol (U2 single stocks, ETFs excluded). Signal A: top-quintile reaction-day
return minus SPY (threshold fixed on tune events). Signal B: EPS surprise > 0 (Finnhub, ~1 year: underpowered).
Entry reaction-day close or next open; hold 1/5/10/20; cost = modeled spread + 2 bp; baseline = equal-weight U2 stocks."""
import json
import os
import pickle
import sys
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = os.getcwd()
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402
import edge2_fetch as e2  # noqa: E402

ETF = {"DIA", "GLD", "IWM", "QQQ", "SMH", "SPY", "TLT", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
STK = sorted(set(e2.U2) - ETF)
FH = os.path.join(ROOT, "ai_reports", "edge5", "fh_earn.json")


def fetch():
    import requests
    from config import load_config
    k = (load_config() or {}).get("finnhub_key")
    out = json.load(open(FH)) if os.path.exists(FH) else {}
    for s in STK:
        if s in out:
            continue
        r = requests.get("https://finnhub.io/api/v1/stock/earnings", params={"symbol": s, "token": k}, timeout=20)
        out[s] = r.json() if r.status_code == 200 else []
        json.dump(out, open(FH, "w"))
        time.sleep(1.2)
    print("finnhub earnings:", len(out), "symbols,", sum(len(v) for v in out.values()), "rows")


def main():
    D = pickle.load(open("ai_reports/edge5/daily_long.pkl", "rb"))
    U = STK + ["SPY"]
    mk = lambda j: pd.DataFrame({s: pd.Series({str(r[0])[:10]: r[j] for r in D[s]}) for s in U}).sort_index()
    O, C, V = mk(1), mk(2), mk(3)
    dates = list(C.index); n = len(dates)
    sm = ec.SpreadModel(set(U))
    cost = pd.Series({s: sm.sym.get(s, 8.0) + 2.0 for s in U})
    rv = V / V.rolling(20).median().shift(1)
    rel = rv.div(rv["SPY"], axis=0)
    ret = C.pct_change() * 1e4
    rex = ret.sub(ret["SPY"], axis=0)
    ev = []
    for s in STK:
        x = rel[s].values
        cand = sorted([(x[i], i) for i in range(21, n - 1) if np.isfinite(x[i]) and x[i] >= 2.5], reverse=True)
        taken = []
        for v, i in cand:
            if all(abs(i - j) >= 40 for j in taken):
                taken.append(i)
        ev += [(dates[i], i, s) for i in sorted(taken)]
    ev.sort()
    yrs = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[21])).days / 365.25
    print(f"T2 inferred reaction days: {len(ev)} events over {yrs:.1f} y, {len(ev)/len(STK)/yrs:.2f} per symbol-year (4.0 expected)")
    m = len(ev)
    part = {k: ("tune" if k < int(m * .6) else "validate" if k < int(m * .8) else "holdout") for k in range(m)}
    cut = [ev[int(m * .6) - 1][0], ev[int(m * .8) - 1][0]]
    print(f"  split by event date: tune ..{cut[0]}, validate ..{cut[1]}, holdout ..{ev[-1][0]}")
    tune_rx = [rex[s].iloc[i] for k, (d, i, s) in enumerate(ev) if part[k] == "tune" and np.isfinite(rex[s].iloc[i])]
    q80, q20 = np.percentile(tune_rx, 80), np.percentile(tune_rx, 20)
    print(f"  top-quintile threshold (tune): reaction minus SPY >= {q80:+.0f} bp; bottom quintile <= {q20:+.0f} bp")
    # EPS surprise matching
    eps = {}
    if os.path.exists(FH):
        fh = json.load(open(FH))
        for s, rows in fh.items():
            for r in rows or []:
                if r.get("actual") is None or r.get("estimate") is None:
                    continue
                P = date.fromisoformat(r["period"])
                for d, i, s2 in ev:
                    if s2 == s and P + timedelta(days=10) < date.fromisoformat(d) <= P + timedelta(days=75):
                        eps[(s, i)] = r["actual"] - r["estimate"]; break
        print(f"  Finnhub EPS rows matched to inferred reaction days: {len(eps)} (of {sum(len(v or []) for v in fh.values())})")

    def evalsig(name, pred, ent, h):
        rec = {p: [] for p in ("tune", "validate", "holdout")}
        for k, (d, i, s) in enumerate(ev):
            if not pred(k, d, i, s) or i + h >= n:
                continue
            p0 = C.iloc[i] if ent == "close" else O.iloc[i + 1]
            g = (C.iloc[i + h] / p0 - 1) * 1e4
            if not np.isfinite(g[s]):
                continue
            uni = g[STK].dropna()
            base = float((uni - cost[uni.index]).mean())
            rec[part[k]].append((i, g[s] - cost[s], g[s] - cost[s] - base, g[s] - g["SPY"]))
        cells = []
        for p, v in rec.items():
            if len(v) < 8:
                cells.append(f"{p} n{len(v)}"); continue
            A = np.array(v)
            a, b = ec.stats(A[:, 1], A[:, 0], boot=100), ec.stats(A[:, 2], A[:, 0], boot=100)
            cells.append(f"{p} n{len(v):4d} net {a['net']:+6.1f} ex {b['net']:+6.1f} (t {b['t']:+.2f}) -SPY {A[:,3].mean():+6.1f}")
        print(f"  {name:28} {ent:5} {h:2d}d | " + " | ".join(cells))
        return rec
    fin = lambda s, i: np.isfinite(rex[s].iloc[i])
    for ent in ("open", "close"):
        for h in (1, 5, 10, 20):
            evalsig("A top-quintile reaction", lambda k, d, i, s: fin(s, i) and rex[s].iloc[i] >= q80, ent, h)
    for h in (1, 5, 10, 20):
        evalsig("(info) all reactions", lambda k, d, i, s: True, "open", h)
        evalsig("(info) bottom-quintile", lambda k, d, i, s: fin(s, i) and rex[s].iloc[i] <= q20, "open", h)
    if eps:
        print("  B EPS surprise > 0 (Finnhub last 4 quarters only -> ~1 year, UNDERPOWERED; parts = whatever falls in each split)")
        for ent in ("open", "close"):
            for h in (1, 5, 10, 20):
                evalsig("B EPS surprise>0", lambda k, d, i, s: eps.get((s, i), 0) > 0, ent, h)
        for h in (1, 5, 20):
            evalsig("(info) EPS surprise<=0", lambda k, d, i, s: (s, i) in eps and eps[(s, i)] <= 0, "open", h)
            evalsig("B+A surprise>0 & top-q", lambda k, d, i, s: eps.get((s, i), 0) > 0 and fin(s, i) and rex[s].iloc[i] >= q80, "open", h)


if __name__ == "__main__":
    fetch() if sys.argv[1:] == ["fetch"] else main()
