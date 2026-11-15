#!/usr/bin/env python3
"""trend_assets.py - monthly cross-asset trend rules vs buy-and-hold (docs/studies/trend_assets_prereg.json).

Decide at the last close of each month, trade at the next open, hold to the next decision. 5 bp on every dollar moved
(weights drift between rebalances, so turnover is measured on drifted weights). Cash = BIL.

    .venv/bin/python tools/studies/trend_assets.py          # mini (Alpaca keys)
"""
from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
FUNDS = ("SPY", "EFA", "IEF", "VNQ", "DBC", "AGG", "BIL")
R1_SET = ("SPY", "EFA", "IEF", "VNQ", "DBC")
COST = 5e-4
START_K = 12                     # both rules need 12 month-ends of history (R1 needs 10); one common start
SPLIT = "2021-11"


def month_ends(days: list[str]) -> list[int]:
    return [i for i in range(len(days) - 1) if days[i][:7] != days[i + 1][:7]]


def period_returns(bars: dict, days: list[str], me: list[int]) -> list[dict]:
    """For decision k (at the close of days[me[k]]): each fund's return from the open after me[k] to the open after me[k+1]."""
    out = []
    for k in range(len(me) - 1):
        a, b = days[me[k] + 1], days[me[k + 1] + 1]
        out.append({f: bars[f][b][0] / bars[f][a][0] - 1 for f in FUNDS})
    return out


def closes_at(bars: dict, days: list[str], me: list[int], f: str) -> list[float]:
    return [bars[f][days[i]][1] for i in me]


# ---------- rules: weights decided at month-end k from closes up to k ----------
def rule_faber(cl: dict, k: int) -> dict:
    w = {}
    for f in R1_SET:
        sma = statistics.fmean(cl[f][k - 9:k + 1])
        w[f if cl[f][k] > sma else "BIL"] = w.get(f if cl[f][k] > sma else "BIL", 0) + 1 / len(R1_SET)
    return w


def rule_dual(cl: dict, k: int) -> dict:
    r = {f: cl[f][k] / cl[f][k - 12] - 1 for f in ("SPY", "EFA", "BIL")}
    if r["SPY"] <= r["BIL"]:
        return {"AGG": 1.0}
    return {"SPY": 1.0} if r["SPY"] >= r["EFA"] else {"EFA": 1.0}


def fixed(w: dict):
    return lambda cl, k: dict(w)


RULES = {
    "R1 Faber 10-month": rule_faber,
    "R2 dual momentum": rule_dual,
    "B1 SPY buy-and-hold": fixed({"SPY": 1.0}),
    "B2 60/40 SPY/IEF": fixed({"SPY": 0.6, "IEF": 0.4}),
    "B3 five funds equal": fixed({f: 1 / len(R1_SET) for f in R1_SET}),
}


def simulate(rule, cl: dict, rets: list[dict], start: int, lag: int = 0, buy_and_hold: bool = False) -> list[float]:
    """Monthly net returns for decisions start..end. lag shifts every decision later by `lag` months (a timing check).
    buy_and_hold: never rebalance after the first month (drifted weights kept)."""
    out, cur = [], {}
    for k in range(start, len(rets)):
        target = rule(cl, k - lag) if not (buy_and_hold and cur) else cur
        turn = sum(abs(target.get(f, 0) - cur.get(f, 0)) for f in set(target) | set(cur)) if cur else 1.0
        g = sum(w * rets[k][f] for f, w in target.items())
        out.append(g - COST * turn)
        tot = sum(w * (1 + rets[k][f]) for f, w in target.items())
        cur = {f: w * (1 + rets[k][f]) / tot for f, w in target.items()}      # drifted weights for next month's turnover
    return out


def stats(r: list[float], cash: list[float]) -> dict:
    if not r:
        return {}
    v, peak, dd = 1.0, 1.0, 0.0
    for x in r:
        v *= 1 + x
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    ex = [a - b for a, b in zip(r, cash)]
    sd = statistics.stdev(ex) if len(ex) > 1 else 0
    return {"cagr": v ** (12 / len(r)) - 1, "dd": dd, "sharpe": statistics.fmean(ex) / sd * math.sqrt(12) if sd > 0 else float("nan"),
            "months": len(r)}


def years(r: list[float], labels: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for x, lab in zip(r, labels):
        out[lab[:4]] = (1 + out.get(lab[:4], 0)) * (1 + x) - 1
    return out


def verdict(name: str, s: dict, halves: tuple[dict, dict], spy: dict, sixty: dict, b3: dict | None) -> str:
    ok = s["dd"] >= spy["dd"] * (2 / 3) and s["sharpe"] >= sixty["sharpe"] and s["cagr"] >= sixty["cagr"] - 0.01
    if b3 is not None:
        ok = ok and s["sharpe"] > b3["sharpe"]
    if not ok:
        return "NOT WORTH IT"
    return "FRAGILE" if any(h["dd"] < hs["dd"] * (2 / 3) for h, hs in zip(halves, spy["halves"])) else "WORTH RUNNING"


def main() -> int:
    import pick_ledger as P
    bars = P.fetch(list(FUNDS), datetime(2015, 12, 1, tzinfo=P.ET))
    days = sorted(set.intersection(*(set(bars[f]) for f in FUNDS)))
    me = month_ends(days)
    rets = period_returns(bars, days, me)
    cl = {f: closes_at(bars, days, me, f) for f in FUNDS}
    labels = [days[me[k] + 1] for k in range(len(rets))]            # entry day of each holding month
    lab = labels[START_K:]
    cash = [x["BIL"] for x in rets[START_K:]]
    h1 = [i for i, d in enumerate(lab) if d[:7] < SPLIT]
    h2 = [i for i, d in enumerate(lab) if d[:7] >= SPLIT]
    print(f"funds {', '.join(FUNDS)}; prices {days[0]}..{days[-1]}; test months {lab[0][:7]}..{lab[-1][:7]} ({len(lab)}); cost {COST * 1e4:.0f} bp per dollar moved\n")

    res = {}
    for name, fn in RULES.items():
        r = simulate(fn, cl, rets, START_K, buy_and_hold=name.startswith("B1"))
        s = stats(r, cash)
        s["halves"] = (stats([r[i] for i in h1], [cash[i] for i in h1]), stats([r[i] for i in h2], [cash[i] for i in h2]))
        s["years"] = years(r, lab)
        s["r"] = r
        res[name] = s

    # checks
    spy_r = res["B1 SPY buy-and-hold"]["r"]
    a, b = days[me[START_K] + 1], days[me[len(rets)] + 1]
    raw = (bars["SPY"][b][0] / bars["SPY"][a][0]) ** (12 / len(spy_r)) - 1
    bil_only = stats(simulate(fixed({"BIL": 1.0}), cl, rets, START_K), cash)
    print("CHECKS")
    print(f"  SPY buy-and-hold {100 * res['B1 SPY buy-and-hold']['cagr']:.2f}%/yr vs SPY's own price path {100 * raw:.2f}%/yr "
          f"({'OK' if abs(res['B1 SPY buy-and-hold']['cagr'] - raw) < 0.002 else 'MISMATCH'})")
    print(f"  all-cash rule {100 * bil_only['cagr']:.2f}%/yr, worst drop {100 * bil_only['dd']:.2f}% ({'OK' if bil_only['dd'] > -0.01 else 'CHECK'})")
    for name in ("R1 Faber 10-month", "R2 dual momentum"):
        late = stats(simulate(RULES[name], cl, rets, START_K + 1, lag=1), cash[1:])
        print(f"  {name} decided one month late: Sharpe {late['sharpe']:.2f} vs on time {res[name]['sharpe']:.2f}")
    print()

    print(f"{'':<22}{'per year':>9}{'worst drop':>12}{'Sharpe':>8}   halves: per year / worst drop")
    for name, s in res.items():
        hh = "   ".join(f"{100 * h['cagr']:+.1f}% / {100 * h['dd']:.0f}%" for h in s["halves"])
        print(f"{name:<22}{100 * s['cagr']:>+8.1f}%{100 * s['dd']:>11.1f}%{s['sharpe']:>8.2f}   {hh}")
    print("\nCALENDAR YEARS")
    ys = sorted(res["B1 SPY buy-and-hold"]["years"])
    print(f"{'':<22}" + "".join(f"{y:>8}" for y in ys))
    for name, s in res.items():
        print(f"{name:<22}" + "".join(f"{100 * s['years'][y]:>+7.1f}%" for y in ys))
    spy, sixty, b3 = res["B1 SPY buy-and-hold"], res["B2 60/40 SPY/IEF"], res["B3 five funds equal"]
    print("\nVERDICT (prereg: worst drop at least 1/3 smaller than SPY's, Sharpe >= 60/40, return within 1 point of 60/40; R1 must beat B3)")
    for name in ("R1 Faber 10-month", "R2 dual momentum"):
        print(f"  {name}: {verdict(name, res[name], res[name]['halves'], spy, sixty, b3 if name.startswith('R1') else None)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
