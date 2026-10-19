#!/usr/bin/env python3
"""Which extra risk is worth taking. Spec: docs/studies/risk_compare_prereg.json. Description, not an edge test.

SPY held / with the 10-month trend exit / 1.5x with and without it, 2000-2026; the R1 momentum book with and
without its 10% trailing stop, 2017-2026. Reads data/yahoo_pre2016/raw/*.pkl (SPY) and ai_reports/gapdown/daily.pkl.

  .venv/bin/python tools/studies/risk_compare.py
"""
from __future__ import annotations

import math
import os
import pickle
import statistics as st
import sys

ROOT = os.environ.get("REPO") or os.getcwd()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
RAW = os.path.join(ROOT, "data", "yahoo_pre2016", "raw")
RESULT = os.path.join(ROOT, "ai_reports", "risk_compare_result.md")

TREND_N = 10
LEV, FIN = 1.5, 0.04
SWITCH_BP = 2.0
W2 = ("2000-01", "2026-09")
NAMES = ("S0_spy", "S1_spy_trend", "S2_spy_1p5x", "S3_spy_1p5x_trend", "S4_momentum", "S5_momentum_trail10")


# ── pure parts (tested) ──────────────────────────────────────────────────────

def month_returns(days: list[str], adj: list[float], lev: float = 1.0, fin: float = 0.0) -> dict[str, float]:
    """{YYYY-MM: return of that calendar month} from daily adjusted closes, levered daily."""
    out: dict[str, float] = {}
    eq, cur = 1.0, None
    for i in range(1, len(days)):
        m = days[i][:7]
        if cur is None:
            cur = m
        if m != cur:
            out[cur] = eq - 1.0
            eq, cur = 1.0, m
        r = adj[i] / adj[i - 1] - 1.0
        eq *= 1.0 + lev * r - (lev - 1.0) * fin / 252.0
    if cur is not None:
        out[cur] = eq - 1.0
    return out


def trend_in(month_end_close: dict[str, float], n: int = TREND_N) -> dict[str, bool]:
    """{YYYY-MM: hold during month M} from the close at the end of the PREVIOUS month vs the mean of the n
    month-end closes ending there."""
    ms = sorted(month_end_close)
    out = {}
    for i in range(n, len(ms)):
        window = [month_end_close[m] for m in ms[i - n:i]]
        out[ms[i]] = month_end_close[ms[i - 1]] > st.fmean(window)
    return out


def apply_trend(rets: dict[str, float], hold: dict[str, bool], cost_bp: float) -> tuple[dict[str, float], int]:
    out, prev, switches = {}, None, 0
    for m in sorted(rets):
        if m not in hold:
            continue
        h = hold[m]
        r = rets[m] if h else 0.0
        if prev is not None and h != prev:
            r -= cost_bp / 1e4
            switches += 1
        out[m] = r
        prev = h
    return out, switches


def stats(rets: list[float], labels: list[str]) -> dict:
    if not rets:
        return {}
    eq, peak, mdd, under, longest = 1.0, 1.0, 0.0, 0, 0
    curve = []
    for r in rets:
        eq *= 1.0 + r
        curve.append(eq)
        if eq >= peak:
            peak, under = eq, 0
        else:
            under += 1
            longest = max(longest, under)
        mdd = min(mdd, eq / peak - 1.0)
    years = len(rets) / 12.0
    cagr = eq ** (1.0 / years) - 1.0
    vol = st.stdev(rets) * math.sqrt(12) if len(rets) > 1 else None
    by_year: dict[str, float] = {}
    for r, lab in zip(rets, labels):
        by_year[lab[:4]] = (1.0 + by_year.get(lab[:4], 0.0)) * (1.0 + r) - 1.0
    return {"cagr": cagr, "vol": vol, "ratio": (cagr / vol) if vol else None, "worst_month": min(rets),
            "worst_year": min(by_year.values()), "mdd": mdd, "longest_under": longest,
            "pos": sum(1 for r in rets if r > 0) / len(rets), "end": 10_000 * eq, "months": len(rets)}


# ── data ─────────────────────────────────────────────────────────────────────

def spy_daily():
    import pandas as pd  # noqa: F401  (the pickles hold DataFrames)
    for fn in sorted(os.listdir(RAW)):
        d = pickle.load(open(os.path.join(RAW, fn), "rb"))
        if "SPY" in d and d["SPY"] is not None:
            df = d["SPY"].dropna(subset=["Adj Close"])
            idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
            return [x.strftime("%Y-%m-%d") for x in idx], [float(v) for v in df["Adj Close"]]
    raise SystemExit(f"no SPY frame under {RAW}")


def momentum_rows():
    """R1 from monthly_book, labelled by the calendar month the holding covers (the month after t)."""
    import monthly_book as M
    p = M.Panel(pickle.load(open(M.DAILY, "rb")))
    out, _ = M.run(p)
    s4, s5 = {}, {}
    for m in out["R1_smooth_momentum"]:
        lab = m["t2"][:7]
        s4[lab] = m["net"]
        s5[lab] = m["stop_net"]["trail10"]
    return s4, s5


def build() -> dict[str, dict[str, float]]:
    days, adj = spy_daily()
    s0 = month_returns(days, adj)
    s2 = month_returns(days, adj, LEV, FIN)
    me = {}
    for d, a in zip(days, adj):
        me[d[:7]] = a                      # last close of each month
    hold = trend_in(me)
    s1, sw1 = apply_trend(s0, hold, SWITCH_BP)
    s3, _ = apply_trend(s2, hold, SWITCH_BP * LEV)
    s4, s5 = momentum_rows()
    series = {"S0_spy": s0, "S1_spy_trend": s1, "S2_spy_1p5x": s2, "S3_spy_1p5x_trend": s3,
              "S4_momentum": s4, "S5_momentum_trail10": s5}
    series["_meta"] = {"switches": sw1, "cash_months": sum(1 for v in hold.values() if not v), "hold": hold,
                       "spy_first": days[0], "spy_last": days[-1]}
    return series


def report(series: dict) -> str:
    meta = series.pop("_meta")
    lines = ["# Risk comparison (risk_compare_prereg)", "",
             f"SPY daily {meta['spy_first']}..{meta['spy_last']}. Description only: no PASS or FAIL. Cash earns 0%.", ""]
    # Labels are the calendar month a holding covers; the momentum book bought at a month-end covers the next month.
    w1_months = [m for m in sorted(series["S4_momentum"]) if all(m in series[n] for n in NAMES)]
    for title, months, names in (
            (f"W1 common window {w1_months[0]}..{w1_months[-1]} ({len(w1_months)} months)", w1_months, NAMES),
            (f"W2 long window {W2[0]}..{W2[1]}", None, NAMES[:4])):
        lines += [f"## {title}", "",
                  "| strategy | CAGR | volatility | CAGR/vol | worst month | worst year | max drawdown | longest under water (months) | months up | $10k becomes |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for n in names:
            ms = months if months is not None else [m for m in sorted(series[n]) if W2[0] <= m <= W2[1]]
            s = stats([series[n][m] for m in ms], ms)
            lines.append(f"| {n} | {s['cagr']:+.1%} | {s['vol']:.1%} | {s['ratio']:.2f} | {s['worst_month']:+.1%} | "
                         f"{s['worst_year']:+.1%} | {s['mdd']:+.1%} | {s['longest_under']} | {s['pos']:.0%} | ${s['end']:,.0f} |")
        lines.append("")
    hold = meta["hold"]
    lines += [f"Trend rule (S1/S3): {meta['switches']} switches, {meta['cash_months']} months in cash since "
              f"{min(hold)}; cash months 2000-2026: " + ", ".join(m for m in sorted(hold) if not hold[m] and m >= W2[0])[:2000], ""]
    return "\n".join(lines)


def main() -> None:
    md = report(build())
    print(md, flush=True)
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
