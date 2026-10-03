#!/usr/bin/env python3
"""counterfactual_arms.py — what the 2026-10-03 changes would have done on recorded sessions.

Takes replay_session.py results (DAY-VARIANT.json; variant A = config as it was, D = spread cap 0.05% and the
momentum exemption off) and, for every replayed trade, prices both legs against the real SIP book:

  entry  market     buy at the SIP ask at entry
         wait       the "wait" arm: if the SIP spread at entry is > WAIT_MIN_BP, take the first quote within
                    10 s whose spread <= 0.75x (or 1 cent) and buy at its ask; else the ask at 10 s
                    (spread <= WAIT_MIN_BP: buy at once)
  exit   market     sell at the SIP bid at exit
         mid        the "mid" exit arm: rest a sell limit at the mid rounded up; filled if a SIP trade prints
                    strictly above it within 10 s; else sell at the SIP bid 10 s later (no floor modelled)

  move   replay ret (recorded price to recorded price, the desk's own exits) — the same for every combination
  net    move - (entry price - entry mid)/entry mid - (exit mid - exit price)/exit mid, in bp

The live A/B splits trades across arms, so the realistic outcome sits between the market/market row and the
all-arms row. Caveats: replay fills ignore queue/latency, the live arms read IEX quotes (wider than SIP, so
the live wait arm fires more often than here), and these are 4 recorded days.

USAGE (mini):  .venv/bin/python tools/studies/counterfactual_arms.py /tmp/rp/2026-*-A.json /tmp/rp/2026-*-D.json
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402

CACHE = os.path.join(ROOT, "ai_reports", "counterfactual_arms_cache.json")
WAIT, RATIO, WAIT_MIN_BP, REST = 10.0, 0.75, 5.0, 10.0


def quotes(cl, sym, t0, secs):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    start = datetime.fromtimestamp(t0, timezone.utc)
    q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=start - timedelta(seconds=30),
                                               end=start + timedelta(seconds=secs), feed=DataFeed.SIP, limit=10000))
    return [(r.timestamp.timestamp(), float(r.bid_price), float(r.ask_price)) for r in (q.data.get(sym) or [])
            if r.bid_price and r.ask_price and r.ask_price >= r.bid_price]


def prints(cl, sym, t0, secs):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    start = datetime.fromtimestamp(t0, timezone.utc)
    q = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=start,
                                               end=start + timedelta(seconds=secs), feed=DataFeed.SIP, limit=10000))
    return [(r.timestamp.timestamp(), float(r.price)) for r in (q.data.get(sym) or [])]


def at(qs, t):
    """Last quote at or before t."""
    best = None
    for q in qs:
        if q[0] <= t:
            best = q
        else:
            break
    return best


def main():
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = None
    rows = defaultdict(list)
    for path in sys.argv[1:]:
        name = os.path.basename(path)[:-5]
        day, var = name[:10], name[11:]
        for t in json.load(open(path)).get("closed") or []:
            if t.get("ret") is None or not t.get("exit_ts"):
                continue
            sym, te, tx = t["symbol"], float(t["entry_ts"]), float(t["exit_ts"])
            k = f"{sym}|{te:.0f}|{tx:.0f}"
            if k not in cache:
                cl = cl or bars.client()
                try:
                    cache[k] = {"qe": quotes(cl, sym, te, WAIT + 1), "qx": quotes(cl, sym, tx, REST + 1),
                                "px": prints(cl, sym, tx, REST)}
                except Exception as e:  # noqa: BLE001
                    print(f"  skip {sym}: {str(e)[:60]}", file=sys.stderr)
                    cache[k] = None
                time.sleep(0.35)   # the live engine shares these data keys
            c = cache[k]
            if not c:
                continue
            e0, x0 = at(c["qe"], te), at(c["qx"], tx)
            if not e0 or not x0 or e0[2] <= e0[1] or x0[2] <= x0[1]:
                continue
            me, mx = (e0[1] + e0[2]) / 2, (x0[1] + x0[2]) / 2
            spr = (e0[2] - e0[1]) / me * 1e4
            # entry, market vs wait
            ent_mkt = e0[2]
            ent_wait, waited = e0[2], False
            if spr > WAIT_MIN_BP:
                win = [q for q in c["qe"] if te < q[0] <= te + WAIT]
                hit = [q for q in win if q[2] - q[1] <= max(RATIO * (e0[2] - e0[1]), 0.0100001)]
                ent_wait = hit[0][2] if hit else (win[-1][2] if win else e0[2])
                waited = True
            # exit, market vs mid
            ex_mkt = x0[1]
            lim = math.ceil(mx * 100 - 1e-9) / 100
            filled = any(p > lim for tt, p in c["px"] if tt - tx <= REST)
            later = at(c["qx"], tx + REST)
            ex_mid = lim if filled else (later[1] if later else x0[1])
            move = t["ret"] * 1e4
            ce = lambda px: (px / me - 1) * 1e4
            cx = lambda px: (1 - px / mx) * 1e4
            rows[var].append({"day": day, "spr": spr, "waited": waited, "filled": filled, "move": move,
                              "mm": move - ce(ent_mkt) - cx(ex_mkt), "wm": move - ce(ent_wait) - cx(ex_mkt),
                              "mx": move - ce(ent_mkt) - cx(ex_mid), "wx": move - ce(ent_wait) - cx(ex_mid)})
        json.dump(cache, open(CACHE, "w"))

    def dct(xs, k):
        days = sorted({x["day"] for x in xs})
        dm = [statistics.mean([x[k] for x in xs if x["day"] == d]) for d in days]
        t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
             if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
        return statistics.mean(x[k] for x in xs), t, len(days)

    print("COUNTERFACTUAL: replayed trades priced against the SIP book (bp per trade; t across days)\n")
    for var in sorted(rows):
        xs = rows[var]
        nd = len({x["day"] for x in xs})
        print(f"  variant {var}: {len(xs)} trades ({len(xs) / nd:.0f}/day), median entry spread "
              f"{statistics.median(x['spr'] for x in xs):.1f} bp, wait arm would act on "
              f"{sum(x['waited'] for x in xs) / len(xs):.0%}, mid exit fills {sum(x['filled'] for x in xs) / len(xs):.0%}")
        print(f"    {'entry / exit':<22}{'net':>8}{'t':>7}{'$/day @1k':>11}")
        for k, lab in (("mm", "market / market"), ("wm", "wait   / market"), ("mx", "market / mid"),
                       ("wx", "wait   / mid (all arms)")):
            m, t, _ = dct(xs, k)
            print(f"    {lab:<22}{m:>+8.1f}{t:>+7.2f}{sum(x[k] for x in xs) / 1e4 * 1000 / nd:>+11.2f}")
        print(f"    move (no costs)       {statistics.mean(x['move'] for x in xs):>+8.1f}\n")


if __name__ == "__main__":
    main()
