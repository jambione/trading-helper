#!/usr/bin/env python3
"""Second warehouse universe for tools/studies/harness.py: cheaper, more volatile names (operator 11/10).

Rule, fixed before any result: for session D a name qualifies when, on the PRIOR session, its raw close was $2 to under $20,
its 20-session mean dollar volume was at least $5M, and it ranks in the top 300 by that prior session's (high - low) / close.
Common stock only (same asset filter as the liquid warehouse). RTH 1-minute SIP bars, split-adjusted.

Each DAY.npz also holds the names that qualify for the NEXT session (the harness needs yesterday's bars for them) and a
`uni` flag marking which names qualify for THIS day, so a name picked by today's range can never be traded today.

    .venv/bin/python tools/studies/warehouse_small.py --sessions 121      # mini, after 16:30; data-only Alpaca account
Writes ai_reports/warehouse/sip_small/DAY.npz and ai_reports/warehouse/small_daily.pkl (the daily table, cached).
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
PX_LO, PX_HI, DV_MIN, DV_DAYS, TOP_N = 2.0, 20.0, 5e6, 20, 300


def pick(daily: dict, days: list[str]) -> dict[str, list[str]]:
    """daily = {sym: {day: (high, low, close, volume)}} raw; days in order. -> {day: names that qualify FOR that day}."""
    out: dict[str, list[str]] = {}
    for i in range(DV_DAYS, len(days)):
        prior, window = days[i - 1], days[i - DV_DAYS:i]
        cand = []
        for sym, rows in daily.items():
            r = rows.get(prior)
            if not r:
                continue
            h, l, c, _v = r
            if not (PX_LO <= c < PX_HI) or c <= 0:
                continue
            dv = [rows[d][2] * rows[d][3] for d in window if d in rows]
            if len(dv) < DV_DAYS or sum(dv) / len(dv) < DV_MIN:
                continue
            cand.append(((h - l) / c, sym))
        cand.sort(reverse=True)
        out[days[i]] = sorted(s for _r, s in cand[:TOP_N])
    return out


def fetch_daily(last: str, n_sessions: int) -> tuple[dict, list[str]]:
    import gapdown_short as G
    from alpaca.data.enums import Adjustment
    from alpaca.data.timeframe import TimeFrame
    cache = os.path.join(OUT, "small_daily.pkl")
    if os.path.exists(cache):
        d = pickle.load(open(cache, "rb"))
        if d["last"] == last and d["n"] >= n_sessions:
            return d["daily"], d["days"]
    cal = sorted(x for x in pickle.load(open(os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl"), "rb"))["adj"]["SPY"]
                 if x <= last)
    days = cal[-(n_sessions + DV_DAYS + 1):]
    y0, m0, d0 = map(int, days[0].split("-"))
    y1, m1, d1 = map(int, last.split("-"))
    syms, cl, log = G._universe(), G._client(), []
    daily: dict[str, dict] = {}
    for i in range(0, len(syms), 50):
        data = G._bars(cl, syms[i:i + 50], TimeFrame.Day, datetime(y0, m0, d0), datetime(y1, m1, d1, 23, 59),
                       Adjustment.RAW, log)
        if data is None:
            raise SystemExit(f"daily fetch failed: {log[-1]}")
        for s, bars in data.items():
            daily[s] = {b.timestamp.strftime("%Y-%m-%d"): (b.high, b.low, b.close, b.volume) for b in bars}
        if (i // 50) % 20 == 0:
            print(f"  daily {i}/{len(syms)}", flush=True)
    os.makedirs(OUT, exist_ok=True)
    pickle.dump({"daily": daily, "days": days, "last": last, "n": n_sessions}, open(cache, "wb"))
    return daily, days


def main() -> None:
    import warehouse_fetch as W
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=121)
    ap.add_argument("--last", default="2026-10-15")
    a = ap.parse_args()
    daily, days = fetch_daily(a.last, a.sessions)
    uni = pick(daily, days)
    sess = sorted(uni)[-a.sessions:]
    out = os.path.join(OUT, "sip_small")
    os.makedirs(out, exist_ok=True)
    jobs = [d for d in sess if not os.path.exists(os.path.join(out, f"{d}.npz"))]
    print(f"sip_small: {len(sess)} sessions {sess[0]}..{sess[-1]}, {len(jobs)} to fetch, "
          f"~{sum(len(uni[d]) for d in sess) // len(sess)} names/day", flush=True)
    cl = W._client()

    def run(i_d):
        d = i_d
        k = sess.index(d)
        nxt = uni[sess[k + 1]] if k + 1 < len(sess) else []
        names = sorted(set(uni[d]) | set(nxt))
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
            if pk is None or n % 10 == 0 or n == len(jobs):
                print(f"  {d}: {'INCOMPLETE ' + (log[-1] if log else '') if pk is None else len(pk['syms'])} ({n}/{len(jobs)})",
                      flush=True)
    print("sip_small done", flush=True)


if __name__ == "__main__":
    main()
