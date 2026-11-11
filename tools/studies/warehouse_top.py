#!/usr/bin/env python3
"""Long history for the largest names (docs/studies/vwap_deep_history_prereg.json).

For each session in [--first, --last]: the N common stocks with the largest PRIOR-session dollar volume (raw close >= $10),
SIP 1-minute RTH bars, split-adjusted, into ai_reports/warehouse/sip_top/DAY.npz. Each file also holds the next session's
names (the harness needs yesterday's bars for them) and a `uni` flag for the names that qualify for THIS day.

    .venv/bin/python tools/studies/warehouse_top.py --first 2024-01-02 --last 2026-04-24      # mini, data-only Alpaca account
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools", "studies")]
OUT = os.path.join(ROOT, "ai_reports", "warehouse")


def pick(daily: dict, days: list[str], n: int, px_min: float = 10.0) -> dict[str, list[str]]:
    """daily = {sym: {day: (close, volume)}} raw. -> {day: the n largest by the PRIOR session's dollar volume}."""
    out = {}
    for i in range(1, len(days)):
        prior = days[i - 1]
        cand = [(r[0] * r[1], s) for s, rows in daily.items() if (r := rows.get(prior)) and r[0] >= px_min]
        cand.sort(reverse=True)
        out[days[i]] = sorted(s for _dv, s in cand[:n])
    return out


def fetch_daily(first: str, last: str) -> tuple[dict, list[str]]:
    import gapdown_short as G
    from alpaca.data.enums import Adjustment
    from alpaca.data.timeframe import TimeFrame
    cache = os.path.join(OUT, f"top_daily_{first}_{last}.pkl")
    if os.path.exists(cache):
        d = pickle.load(open(cache, "rb"))
        return d["daily"], d["days"]
    table = pickle.load(open(os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl"), "rb"))
    cal = sorted(table["adj"]["SPY"])
    days = [x for x in cal if x <= last]
    days = days[max(0, next(i for i, x in enumerate(days) if x >= first) - 1):]
    syms = sorted(table["liquid"])                       # every name that was ever liquid 2016-2026 (active and delisted)
    y0, m0, d0 = map(int, days[0].split("-"))
    y1, m1, d1 = map(int, last.split("-"))
    cl, log, daily = G._client(), [], {}
    for i in range(0, len(syms), 15):                    # 15 names x ~600 sessions < the 10k-bar page
        data = G._bars(cl, syms[i:i + 15], TimeFrame.Day, datetime(y0, m0, d0), datetime(y1, m1, d1, 23, 59), Adjustment.RAW, log)
        if data is None:
            raise SystemExit(f"daily fetch failed: {log[-1]}")
        for s, bars in data.items():
            daily[s] = {b.timestamp.strftime("%Y-%m-%d"): (b.close, b.volume) for b in bars}
        if (i // 15) % 40 == 0:
            print(f"  daily {i}/{len(syms)}", flush=True)
    pickle.dump({"daily": daily, "days": days}, open(cache, "wb"))
    return daily, days


def main() -> None:
    import warehouse_fetch as W
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", default="2024-01-02")
    ap.add_argument("--last", default="2026-04-24")
    ap.add_argument("--n", type=int, default=60)
    a = ap.parse_args()
    daily, days = fetch_daily(a.first, a.last)
    uni = pick(daily, days, a.n)
    sess = sorted(uni)
    out = os.path.join(OUT, "sip_top")
    os.makedirs(out, exist_ok=True)
    jobs = [d for d in sess if not os.path.exists(os.path.join(out, f"{d}.npz"))]
    print(f"sip_top: {len(sess)} sessions {sess[0]}..{sess[-1]}, {len(jobs)} to fetch", flush=True)
    cl = W._client()

    def run(d):
        k = sess.index(d)
        names = sorted(set(uni[d]) | set(uni[sess[k + 1]] if k + 1 < len(sess) else []))
        pk, log = W.fetch_day(cl, d, names, "sip")
        if pk is not None:
            today = set(uni[d])
            pk["uni"] = np.array([str(s) in today for s in pk["syms"]], dtype=bool)
            path = os.path.join(out, f"{d}.npz")
            np.savez_compressed(path + ".tmp.npz", **pk)
            os.replace(path + ".tmp.npz", path)
        return d, pk, log

    with ThreadPoolExecutor(W.WORKERS) as ex:
        for n, (d, pk, log) in enumerate(ex.map(run, jobs), 1):
            if pk is None or n % 25 == 0 or n == len(jobs):
                print(f"  {d}: {'INCOMPLETE ' + (log[-1] if log else '') if pk is None else len(pk['syms'])} ({n}/{len(jobs)})", flush=True)
    print("sip_top done", flush=True)


if __name__ == "__main__":
    main()
