#!/usr/bin/env python3
"""freshness_study.py — which freshness rule can the arm trust on IEX data?

On 2026-09-25, 43% of RTH arm checks were refused `tape_only`: the desk's last
IEX trade print was over 15 s old. IEX sees a few percent of all trades, so a
liquid name can trade constantly while its IEX print ages. The fix has to be
judged against the real market, not against itself: for a sample of live's
tape_only refusals, this rebuilds what the desk could have known at that
moment from IEX data, and scores each candidate rule against SIP truth.

  truth      SIP NBBO (latest quote at or before t): ask and mid; and whether
             SIP itself printed a trade in the last 15 s (market alive)
  R0 print   IEX last trade, age <= 15 s (the rule now; these rows all failed it)
  R1 quote   IEX latest quote, age <= 15 s / <= 5 s; price = IEX ask
  R2 adapt   IEX last trade, age <= max(15 s, 3 x the name's median IEX
             inter-trade gap over the prior 30 min); price = that trade
  R3 bar     last completed IEX 1m bar, closed <= 90 s ago; price = its close

For each rule: the share of refusals it would unblock, and for those, the
price error against the SIP ask (what a market buy pays) and the SIP mid, bp.
A rule is usable if it unblocks most refusals with a small error.

USAGE (on the mini, after hours)
  .venv/bin/python tools/studies/freshness_study.py --day 2026-09-25 --n 300
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):  # run from a copy: use the cwd repo
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)


def refusals(day, n, seed=5):
    p = Path.home() / "session_snapshots" / day / "decision_ledger.jsonl"
    seen, rows = set(), []
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("arm_why") != "tape_only":
            continue
        k = (r["symbol"], int(r["ts"] // 120))
        if k in seen:
            continue
        seen.add(k)
        rows.append((float(r["ts"]), str(r["symbol"]).upper(), r.get("tape_age_sec")))
    rng = random.Random(seed)
    return rng.sample(rows, min(n, len(rows)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-09-25")
    ap.add_argument("--n", type=int, default=300)
    args = ap.parse_args()
    import ai_entry_watch as ew
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest, StockQuotesRequest, StockTradesRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = ew._data_client()
    out = []
    samples = refusals(args.day, args.n)
    print(f"{len(samples)} tape_only refusals sampled on {args.day}")
    for k, (t, sym, desk_age) in enumerate(samples):
        end = datetime.fromtimestamp(t, timezone.utc)
        try:
            sq = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=end - timedelta(seconds=10), end=end,
                feed=DataFeed.SIP)).data.get(sym) or []
            st = cl.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=sym, start=end - timedelta(seconds=15), end=end,
                feed=DataFeed.SIP, limit=5)).data.get(sym) or []
            it = cl.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=sym, start=end - timedelta(minutes=30), end=end,
                feed=DataFeed.IEX)).data.get(sym) or []
            iq = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=end - timedelta(seconds=60), end=end,
                feed=DataFeed.IEX)).data.get(sym) or []
            ib = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=end - timedelta(minutes=5), end=end, feed=DataFeed.IEX)).data.get(sym) or []
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: {e}"[:120])
            continue
        sq = [q for q in sq if q.bid_price and q.ask_price and q.ask_price >= q.bid_price]
        if not sq:
            continue
        s_ask = float(sq[-1].ask_price)
        s_mid = (float(sq[-1].ask_price) + float(sq[-1].bid_price)) / 2
        row = {"t": t, "sym": sym, "desk_age": desk_age, "sip_alive": bool(st),
               "sip_ask": s_ask, "sip_mid": s_mid}
        if it:
            last = it[-1]
            row["print_age"] = t - last.timestamp.timestamp()
            row["print_px"] = float(last.price)
            ts = [x.timestamp.timestamp() for x in it]
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            row["med_gap"] = statistics.median(gaps) if gaps else None
        iq = [q for q in iq if q.ask_price and q.bid_price and q.ask_price >= q.bid_price]
        if iq:
            row["quote_age"] = t - iq[-1].timestamp.timestamp()
            row["quote_ask"] = float(iq[-1].ask_price)
        done = [b for b in ib if b.timestamp.timestamp() + 60 <= t]
        if done:
            row["bar_age"] = t - (done[-1].timestamp.timestamp() + 60)
            row["bar_close"] = float(done[-1].close)
        out.append(row)
        if k % 25 == 0:
            print(f"  {k}/{len(samples)}")
            json.dump(out, open(os.path.join(ROOT, "ai_reports", f"freshness_study_{args.day}.json"), "w"))
        time.sleep(0.25)
    json.dump(out, open(os.path.join(ROOT, "ai_reports", f"freshness_study_{args.day}.json"), "w"))

    def err(px, ref):
        return abs(px / ref - 1) * 1e4

    def rule(name, ok, px_key):
        hit = [r for r in out if ok(r)]
        ea = [err(r[px_key], r["sip_ask"]) for r in hit]
        em = [err(r[px_key], r["sip_mid"]) for r in hit]
        dead = sum(1 for r in hit if not r["sip_alive"])
        if not hit:
            print(f"  {name:34} unblocks 0")
            return
        print(f"  {name:34} unblocks {len(hit) / len(out):5.0%}  err vs SIP ask median "
              f"{statistics.median(ea):5.1f} bp p90 {sorted(ea)[int(.9 * len(ea))]:6.1f}  vs mid "
              f"median {statistics.median(em):5.1f} p90 {sorted(em)[int(.9 * len(em))]:6.1f}  "
              f"(SIP quiet too: {dead})")

    alive = sum(1 for r in out if r["sip_alive"])
    print(f"\n{len(out)} scored; the market itself printed within 15 s on {alive / len(out):.0%} "
          f"of them (these refusals were IEX gaps, not quiet markets)\n")
    rule("R0 IEX print <= 15 s (now)", lambda r: r.get("print_age") is not None and r["print_age"] <= 15, "print_px")
    rule("R0' IEX print <= 60 s", lambda r: r.get("print_age") is not None and r["print_age"] <= 60, "print_px")
    rule("R1 IEX quote <= 15 s (ask)", lambda r: r.get("quote_age") is not None and r["quote_age"] <= 15, "quote_ask")
    rule("R1b IEX quote <= 5 s (ask)", lambda r: r.get("quote_age") is not None and r["quote_age"] <= 5, "quote_ask")
    rule("R2 IEX print <= max(15, 3 x gap)", lambda r: r.get("print_age") is not None and r.get("med_gap")
         is not None and r["print_age"] <= max(15, 3 * r["med_gap"]), "print_px")
    rule("R3 IEX 1m bar closed <= 90 s", lambda r: r.get("bar_age") is not None and r["bar_age"] <= 90, "bar_close")


if __name__ == "__main__":
    main()
