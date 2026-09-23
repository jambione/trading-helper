#!/usr/bin/env python3
"""exec_report.py — what each order paid against the TRUE market at submit.

The desk's live quotes are IEX, a few percent of the tape, and their spreads
read far wider than the real book. Historical SIP quotes are available once
they are 15 minutes old, so after the fact every order in the fill ledger can
be priced against the consolidated best bid/offer at the instant it was sent.

Per filled order (ai_reports/fills/<day>.jsonl, folded to the last row per
order_id):
  mid        SIP (bid + ask) / 2 at submitted_at
  half_spr   (ask - bid) / 2 / mid, %  — the unavoidable cost of crossing
  cost       buy: fill / mid - 1;  sell: 1 - fill / mid   (%; + = paid)
  excess     cost - half_spr: what we paid beyond crossing the spread
  delay      filled_at - submitted_at, seconds

USAGE (on the mini, after the close)
    .venv/bin/python tools/exec_report.py                  # today
    .venv/bin/python tools/exec_report.py --day 2026-09-23 --detail
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def orders(day: str) -> list[dict]:
    path = os.path.join(ROOT, "ai_reports", "fills", f"{day}.jsonl")
    fills, submits = {}, {}
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        oid = r.get("order_id")
        if not oid:
            continue
        if r.get("event") == "fill":
            fills[oid] = r
        elif r.get("event") == "submit":
            submits[oid] = r
    out = []
    for oid, f in fills.items():
        if str(f.get("status")) != "filled" or not f.get("filled_avg_price"):
            continue
        sub = submits.get(oid, {})
        out.append({"oid": oid, "sym": f["symbol"], "side": f.get("side"),
                    "type": f.get("type"), "px": float(f["filled_avg_price"]),
                    "qty": f.get("filled_qty"),
                    "t_sub": _ts(f.get("submitted_at")), "t_fill": _ts(f.get("filled_at")),
                    "note": sub.get("note") or ""})
    return sorted(out, key=lambda o: o["t_sub"] or datetime.min.replace(tzinfo=timezone.utc))


def nbbo_at(cl, sym: str, t: datetime):
    """(bid, ask) of the last SIP quote at or before t, or None."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    for back in (2, 30, 300):
        try:
            q = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=t - timedelta(seconds=back),
                end=t + timedelta(milliseconds=1), feed=DataFeed.SIP, limit=10000))
        except Exception as e:  # noqa: BLE001
            print(f"  quotes fail {sym}: {str(e)[:80]}", file=sys.stderr)
            return None
        rows = [r for r in (q.data.get(sym) or [])
                if r.bid_price and r.ask_price and r.ask_price >= r.bid_price]
        if rows:
            r = rows[-1]
            return float(r.bid_price), float(r.ask_price)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(bars.ET).strftime("%Y-%m-%d"))
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    cl = bars.client()
    rows = []
    for o in orders(args.day):
        if not o["t_sub"]:
            continue
        q = nbbo_at(cl, o["sym"], o["t_sub"])
        time.sleep(0.3)   # the live engine shares these data keys
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2
        cost = (o["px"] / mid - 1) * 100 if o["side"] == "buy" else (1 - o["px"] / mid) * 100
        half = (ask - bid) / 2 / mid * 100
        delay = ((o["t_fill"] - o["t_sub"]).total_seconds()
                 if o["t_fill"] and o["t_sub"] else None)
        rows.append({**o, "bid": bid, "ask": ask, "mid": mid, "cost": cost,
                     "half": half, "excess": cost - half, "delay": delay,
                     "collar": "collar" in o["note"]})

    def line(label, xs):
        if not xs:
            return
        m = lambda k: statistics.mean([x[k] for x in xs if x.get(k) is not None])
        med_d = statistics.median([x["delay"] for x in xs if x.get("delay") is not None] or [0])
        print(f"  {label:<26}{len(xs):>4}{m('cost'):>+9.3f}{m('half'):>9.3f}"
              f"{m('excess'):>+9.3f}{med_d:>9.1f}{max(x['delay'] or 0 for x in xs):>8.0f}")

    print(f"EXEC REPORT {args.day}  (vs SIP NBBO at submit; % of mid; + = paid)\n")
    print(f"  {'group':<26}{'n':>4}{'cost':>9}{'half spr':>9}{'excess':>9}{'med s':>9}{'max s':>8}")
    line("ALL", rows)
    line("buys", [r for r in rows if r["side"] == "buy"])
    line("sells", [r for r in rows if r["side"] == "sell"])
    line("sells, capped (collar)", [r for r in rows if r["side"] == "sell" and r["collar"]])
    line("sells, plain market", [r for r in rows if r["side"] == "sell" and not r["collar"]])
    for lo, hi in ((0, 20), (20, 50), (50, 1e9)):
        line(f"price ${lo}-{hi if hi < 1e9 else '+'}", [r for r in rows if lo <= r["px"] < hi])
    total_cost = sum(r["cost"] / 100 * r["px"] * float(r["qty"] or 0) for r in rows)
    print(f"\n  total paid vs mid: ${total_cost:,.2f} across {len(rows)} orders")
    if args.detail:
        print(f"\n  {'time':<9}{'sym':<6}{'side':<5}{'fill':>9}{'bid':>9}{'ask':>9}{'cost%':>8}{'delay s':>9}  note")
        for r in rows:
            t = r["t_sub"].astimezone(bars.ET).strftime("%H:%M:%S")
            print(f"  {t:<9}{r['sym']:<6}{r['side']:<5}{r['px']:>9.3f}{r['bid']:>9.3f}{r['ask']:>9.3f}"
                  f"{r['cost']:>+8.3f}{(r['delay'] or 0):>9.1f}  {r['note'][:40]}")


if __name__ == "__main__":
    main()
