#!/usr/bin/env python3
"""Overnight book exit timing: opening auction vs 09:35/09:45/10:00/10:30.
Pre-registered: docs/studies/overnight_exit_timing_prereg.json. Runs on the mini (longer-holds panel + keys).

usage: .venv/bin/python tools/studies/overnight_exit_timing.py [N_SESSIONS]
"""
import math
import os
import pickle
import statistics as st
import sys
import time
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
sys.path.insert(0, "/tmp/lh")
import bars  # noqa: E402
import lh_core as C  # noqa: E402
import lh_strat as S  # noqa: E402

EXITS = ("09:35", "09:45", "10:00", "10:30")
AUCTION_BP = 2.0
CACHE = os.path.join(ROOT, "ai_reports", "overnight_exit_bars.pkl")


def tstat(v):
    return st.mean(v) / (st.stdev(v) / math.sqrt(len(v))) if len(v) > 2 else float("nan")


def main():
    n_sess = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    P = C.load()
    S.returns(P)
    dts = [str(x)[:10] for x in P["dates"]]
    T = len(dts)
    momd = np.full_like(P["c"], np.nan)
    with np.errstate(all="ignore"):
        momd[252:] = P["cf"][231:-21] / P["cf"][:-252] - 1
    syms = P["syms"]
    nights = []                      # (t, [j...]) bought at close t, sold on t+1
    for t in range(max(252, T - 1 - n_sess), T - 1):
        x = np.where(P["liquid"][t] & np.isfinite(momd[t]), momd[t], np.nan)
        ok = np.where(np.isfinite(x))[0]
        if len(ok) >= 30:
            nights.append((t, list(ok[np.argsort(-x[ok])][:20])))
    cache = pickle.load(open(CACHE, "rb")) if os.path.exists(CACHE) else {}
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    for k, (t, js) in enumerate(nights):
        day = dts[t + 1]
        if day in cache:
            continue
        y, m, d = map(int, day.split("-"))
        s0 = datetime(y, m, d, 9, 30, tzinfo=bars.ET)
        s1 = datetime(y, m, d, 10, 31, tzinfo=bars.ET)
        names = [str(syms[j]) for j in js]
        try:
            got = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=names, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=s0.astimezone(timezone.utc), end=s1.astimezone(timezone.utc), feed=DataFeed.SIP)).data
        except Exception as ex:  # noqa: BLE001
            print("  skip", day, str(ex)[:70], file=sys.stderr)
            time.sleep(2)
            continue
        out = {}
        for sym, rs in (got or {}).items():
            if not rs:
                continue
            first_open = rs[0].open if rs[0].timestamp.astimezone(bars.ET).strftime("%H:%M") == "09:30" else None
            closes = {r.timestamp.astimezone(bars.ET).strftime("%H:%M"): r.close for r in rs}
            out[sym] = (first_open, closes)
        cache[day] = out
        if k % 25 == 0:
            pickle.dump(cache, open(CACHE, "wb"))
            print(f"  bars {k}/{len(nights)} {day}", file=sys.stderr)
        time.sleep(1.0)
    pickle.dump(cache, open(CACHE, "wb"))

    def at_or_before(closes, hhmm):
        ks = [k for k in closes if k <= hhmm]
        return closes[max(ks)] if ks else None

    res = {e: [] for e in ("open",) + EXITS}
    inc = {e: [] for e in EXITS}
    for t, js in nights:
        day = dts[t + 1]
        bd = cache.get(day) or {}
        g = {e: [] for e in ("open",) + EXITS}
        for j in js:
            on = P["ON"][t + 1, j]
            if not np.isfinite(on):
                continue
            sym = str(syms[j])
            fo, closes = bd.get(sym, (None, {}))
            spr = P["spr_o"][t + 1, j] if np.isfinite(P["spr_o"][t + 1, j]) else np.nan
            g["open"].append(on * 1e4 - 2 * AUCTION_BP)
            for e in EXITS:
                c = at_or_before(closes, e) if closes else None
                if not fo or not c or not np.isfinite(spr):
                    g[e].append(None)
                    continue
                tot = ((1 + on) * (c / fo) - 1) * 1e4
                g[e].append(tot - AUCTION_BP - spr * 1e4 / 2)
        # basket per night on names with every exit priced, so exits are compared on the same names
        keep = [i for i in range(len(g["open"])) if all(g[e][i] is not None for e in EXITS)]
        if len(keep) < 10:
            continue
        b_open = st.mean(g["open"][i] for i in keep)
        res["open"].append((day, b_open))
        for e in EXITS:
            b = st.mean(g[e][i] for i in keep)
            res[e].append((day, b))
            inc[e].append((day, b - b_open))

    days = [d for d, _ in res["open"]]
    mid = days[len(days) // 2] if days else ""
    print(f"nights scored {len(days)} ({days[0] if days else ''}..{days[-1] if days else ''}); halves split at {mid}")
    print(f"costs: open exit {2 * AUCTION_BP:.0f} bp round trip; later exits 2 bp + half the modeled 09:35 spread "
          f"(median half-spread {np.nanmedian(P['spr_o'][T - n_sess:]) * 1e4 / 2:.1f} bp)")
    print(f"\n{'exit':8s} {'net/night':>10s} {'t':>6s} {'win':>5s} | {'vs open':>8s} {'t':>6s} | {'older half':>11s} {'newer half':>11s}")
    for e in ("open",) + EXITS:
        v = [x for _, x in res[e]]
        line = f"{e:8s} {st.mean(v):+9.1f}  {tstat(v):+5.2f} {sum(x > 0 for x in v) / len(v):4.0%}"
        if e != "open":
            iv = [x for _, x in inc[e]]
            h1 = [x for d, x in inc[e] if d < mid]
            h2 = [x for d, x in inc[e] if d >= mid]
            line += (f" | {st.mean(iv):+7.1f}  {tstat(iv):+5.2f} | {st.mean(h1):+6.1f} (t {tstat(h1):+.1f})"
                     f" {st.mean(h2):+6.1f} (t {tstat(h2):+.1f})")
        print(line)


if __name__ == "__main__":
    main()
