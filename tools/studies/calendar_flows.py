#!/usr/bin/env python3
"""Calendar-flow family on SPY and IWM. Spec: docs/studies/calendar_flows_prereg.json.

Close-to-close daily returns from Yahoo adjusted closes (as month_end_rebal.py). Each window is scored as
in-window daily return minus out-of-window daily return (Welch t), in both halves (split 2010-01-01), plus the
strategy of holding the ETF only on window days, net of COST_BP per round trip.

  --fetch   download and cache the closes (ai_reports/calendar_flows/closes.pkl)
  --score   score (only after a result-skeptic READY on the prereg)
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import statistics as st
from datetime import date, timedelta

ROOT = os.getcwd()
CACHE = os.path.join(ROOT, "ai_reports", "calendar_flows", "closes.pkl")
SPLIT = date(2010, 1, 1)
T_BAR = 2.6
COST_BP = 2.0
START = {"SPY": "1993-02-01", "IWM": "2000-06-01"}


# ── pure window logic (tested) ────────────────────────────────────────────────

def month_key(d: date) -> tuple[int, int]:
    return d.year, d.month


def window_flags(days: list[date]) -> dict[str, list[bool]]:
    """Per trading day, which windows it belongs to. `days` is the sorted list of trading days."""
    n = len(days)
    idx_in_month: list[int] = []
    from_end: list[int] = []
    # position within the month from the start (1 = first trading day) and from the end (1 = last)
    i = 0
    while i < n:
        j = i
        while j < n and month_key(days[j]) == month_key(days[i]):
            j += 1
        for k in range(i, j):
            idx_in_month.append(k - i + 1)
            from_end.append(j - k)
        i = j
    tom = [from_end[k] == 1 or idx_in_month[k] <= 3 for k in range(n)]
    qe = [from_end[k] <= 2 and days[k].month in (3, 6, 9, 12) for k in range(n)]
    tradeset = set(days)
    prehol = []
    for k in range(n):
        nxt = days[k] + timedelta(days=1)
        # next weekday that is not a trading day = a market holiday (only within the data's span)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        prehol.append(k < n - 1 and nxt not in tradeset)
    taxjan = [(days[k].month == 12 and from_end[k] <= 3) or (days[k].month == 1 and idx_in_month[k] <= 5)
              for k in range(n)]
    opex = []
    for k in range(n):
        d = days[k]
        first = d.replace(day=1)
        third_fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        monday = third_fri - timedelta(days=4)
        opex.append(monday <= d <= third_fri)
    return {"TOM": tom, "QE": qe, "PREHOL": prehol, "TAXJAN": taxjan, "OPEX": opex}


def welch_t(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    va, vb = st.variance(a), st.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (st.fmean(a) - st.fmean(b)) / se if se > 0 else None


def score_cell(days: list[date], rets: list[float], flag: list[bool], lo: date | None, hi: date | None) -> dict:
    """In-minus-out on days in [lo, hi); returns are the day's close-to-close return (day k vs k-1)."""
    ins, outs = [], []
    for k in range(1, len(days)):
        d = days[k]
        if (lo and d < lo) or (hi and d >= hi):
            continue
        (ins if flag[k] else outs).append(rets[k])
    t = welch_t(ins, outs)
    # strategy: hold only in-window days; one round trip per contiguous run
    runs = sum(1 for k in range(1, len(days)) if flag[k] and not flag[k - 1]
               and not ((lo and days[k] < lo) or (hi and days[k] >= hi)))
    strat = sum(ins) - runs * COST_BP / 1e4
    return {"n_in": len(ins), "in_bp": st.fmean(ins) * 1e4 if ins else None,
            "out_bp": st.fmean(outs) * 1e4 if outs else None,
            "diff_bp": (st.fmean(ins) - st.fmean(outs)) * 1e4 if ins and outs else None,
            "t": t, "strategy_net_pct": strat * 100, "round_trips": runs}


# ── data and report ───────────────────────────────────────────────────────────

def fetch() -> dict:
    import yfinance as yf
    out = {}
    for sym, start in START.items():
        df = yf.download(sym, start=start, end="2026-10-16", auto_adjust=True, progress=False)["Close"]
        s = df[sym] if hasattr(df, "columns") else df
        out[sym] = [(i.date(), float(v)) for i, v in s.dropna().items()]
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    pickle.dump(out, open(CACHE, "wb"))
    return out


def score() -> str:
    data = pickle.load(open(CACHE, "rb"))
    lines = ["# Calendar-flow family (Yahoo adjusted closes; INFORMATION until both halves pass)", "",
             f"t bar {T_BAR} (9 cells); cost {COST_BP:g} bp per round trip; halves split {SPLIT}", "",
             "| ETF | window | half | in-window days | in bp/day | out bp/day | diff bp (t) | in-window strategy net % | verdict |",
             "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for sym, rows in data.items():
        days = [d for d, _ in rows]
        px = [p for _, p in rows]
        rets = [0.0] + [px[k] / px[k - 1] - 1 for k in range(1, len(px))]
        flags = window_flags(days)
        for w, flag in flags.items():
            if w == "TAXJAN" and sym != "IWM":
                continue
            halves = [score_cell(days, rets, flag, None, SPLIT), score_cell(days, rets, flag, SPLIT, None)]
            ok = all(h["t"] is not None and h["t"] >= T_BAR and h["strategy_net_pct"] > 0 for h in halves)
            for name, h in zip(("A <2010", "B 2010+"), halves):
                f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
                lines.append(f"| {sym} | {w} | {name} | {h['n_in']} | {f(h['in_bp'])} | {f(h['out_bp'])} | "
                             f"{f(h['diff_bp'])} ({f(h['t'])}) | {h['strategy_net_pct']:+.1f} | {'PASS' if ok else 'FAIL'} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    if a.score:
        out = score()
        print(out)
        with open(os.path.join(os.path.dirname(CACHE), "result.md"), "w") as fh:
            fh.write(out + "\n")


if __name__ == "__main__":
    main()
