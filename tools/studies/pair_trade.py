#!/usr/bin/env python3
"""pair_trade.py - the vwap_deep entry as a PAIR (docs/studies/vwap_deep_pair_prereg.json, operator 11/11).

Long the stretched name, short a hedge for the same dollars, both for HOLD minutes. Hedges: QQQ (primary), SPY, and the
equal-weight basket of the other universe names (the ceiling, not one order). Everything is PER PAIR TRADE and under a
slot limit taken in time order, because day means hid the outright loss (docs/running_list.md row 116).

    .venv/bin/python tools/studies/pair_trade.py [--warehouse ai_reports/warehouse/sip_top] [--hedges ai_reports/warehouse/sip_hedge]
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H
import vwap_deep as V

HOLD, CAP = 60, 500.0


def hedge_fwd(path: Path, hold: int) -> dict[str, np.ndarray]:
    """{'QQQ': fwd[390], 'SPY': fwd[390]} in bp, the same open-to-open window the name trade uses."""
    d = H.load_day(path)
    f = H.forward_returns(d, hold)
    return {s: f[i] for i, s in enumerate(d["syms"])}


def collect(wdir: Path, hdir: Path, top_n: int, hold: int) -> list[dict]:
    files = sorted(wdir.glob("*.npz"))
    out, prev_raw = [], H.load_day(files[0])
    for f in files[1:]:
        raw = H.load_day(f)
        day, prev = H.universe(raw, prev_raw, top_n, 10.0, 5000.0)
        prev_raw = raw
        hp = hdir / f.name
        if not day["syms"] or not hp.exists():
            continue
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            r = H.score_day(day, np.asarray(V.signal(day, prev), bool), hold)
        row = {"day": f.stem, "minute": np.array([], int), "name": np.array([]), "QQQ": np.array([]), "SPY": np.array([]), "peers": np.array([])}
        if r:
            hf = hedge_fwd(hp, hold)
            m = r["minute"]
            row = {"day": f.stem, "minute": m, "name": r["gross"], "QQQ": hf["QQQ"][m], "SPY": hf["SPY"][m], "peers": r["ctl_min"]}
        out.append(row)
    return out


def simulate(days: list[dict], hedge: str | None, slots: int | None, cost: float, hold: int = HOLD) -> dict:
    """Per-trade and per-day results. hedge None = the name alone. slots None = every signal."""
    trades, per_day = [], []
    for d in days:
        pnl, free = [], []
        for i in np.argsort(d["minute"], kind="stable"):
            x = d["name"][i] - (d[hedge][i] if hedge else 0.0)
            if np.isnan(x):
                continue
            if slots is not None:
                free = [t for t in free if t > d["minute"][i]]
                if len(free) >= slots:
                    continue
                free.append(d["minute"][i] + 1 + hold)
            pnl.append(float(np.clip(x, -CAP, CAP)) - cost)
        trades += pnl
        per_day.append(sum(pnl) / (slots or 50))                 # bp on capital when a slot is 1/slots of it
    tr, v = np.array(trades), np.array(per_day)
    h = len(days) // 2
    n1 = sum(1 for d in days[:h] for i in range(len(d["minute"])))   # noqa: F841 (kept simple: halves use the day split below)
    first = simulate_half(days[:h], hedge, slots, cost, hold) if len(days) > 3 and h else float("nan")
    second = simulate_half(days[h:], hedge, slots, cost, hold) if len(days) > 3 and h else float("nan")
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else float("nan")
    return {"n": len(tr), "net": float(tr.mean()) if len(tr) else float("nan"), "win": float(np.mean(tr > 0)) if len(tr) else float("nan"),
            "halves": (first, second), "day": float(v.mean()), "t": float(t), "worst": float(v.min()) if len(v) else float("nan"),
            "sd": float(tr.std()) if len(tr) else float("nan")}


def simulate_half(days, hedge, slots, cost, hold) -> float:
    tot, n = 0.0, 0
    for d in days:
        free = []
        for i in np.argsort(d["minute"], kind="stable"):
            x = d["name"][i] - (d[hedge][i] if hedge else 0.0)
            if np.isnan(x):
                continue
            if slots is not None:
                free = [t for t in free if t > d["minute"][i]]
                if len(free) >= slots:
                    continue
                free.append(d["minute"][i] + 1 + hold)
            tot += float(np.clip(x, -CAP, CAP)) - cost
            n += 1
    return tot / n if n else float("nan")


def show(label: str, r: dict) -> str:
    return (f"  {label:<34}{r['n']:>7}  net/pair {r['net']:>+6.2f}  halves {r['halves'][0]:>+6.2f} / {r['halves'][1]:<+6.2f}  win {100 * r['win']:.0f}%  "
            f"trade sd {r['sd']:.0f}  | per day {r['day']:>+6.2f} bp  day-t {r['t']:>+5.2f}  worst day {r['worst']:+.0f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.ROOT / "ai_reports" / "warehouse" / "sip_top"))
    ap.add_argument("--hedges", default=str(H.ROOT / "ai_reports" / "warehouse" / "sip_hedge"))
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--by-period", action="store_true")
    a = ap.parse_args(argv)
    days = collect(Path(a.warehouse), Path(a.hedges), a.top_n, HOLD)
    print(f"{len(days)} days {days[0]['day']}..{days[-1]['day']}, hold {HOLD} m, trades capped at +-{CAP:g} bp")
    print("PRIMARY (pre-registered): long name / short QQQ, 3 pairs at a time in time order")
    print(show("cost 3 bp", simulate(days, "QQQ", 3, 3.0)))
    print(show("stress cost 8 bp", simulate(days, "QQQ", 3, 8.0)))
    print("SECONDARY, 3 at a time, cost 3 bp (6 bp for the 49-name basket, which is not one order)")
    print(show("name alone, no hedge (3 bp)", simulate(days, None, 3, 3.0)))
    print(show("short SPY", simulate(days, "SPY", 3, 3.0)))
    print(show("short the other largest-50 names", simulate(days, "peers", 3, 6.0)))
    print("SECONDARY, every signal, cost 3 bp")
    print(show("short QQQ", simulate(days, "QQQ", None, 3.0)))
    print(show("short SPY", simulate(days, "SPY", None, 3.0)))
    if a.by_period:
        print("BY PERIOD: long name / short QQQ, 3 at a time, cost 3 bp")
        for lab, lo, hi in (("2024 H1", "2024-01", "2024-07"), ("2024 H2", "2024-07", "2025-01"), ("2025 H1", "2025-01", "2025-07"),
                            ("2025 H2", "2025-07", "2026-01"), ("2026", "2026-01", "2027-01")):
            sub = [d for d in days if lo <= d["day"] < hi]
            if len(sub) > 5:
                print(show(lab, simulate(sub, "QQQ", 3, 3.0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
