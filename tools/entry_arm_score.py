#!/usr/bin/env python3
"""entry_arm_score.py — grade the three-arm entry test (ai_entry_test_arms) against the SIP mid.

Each closed day trade whose outcome carries ``entry_test`` (arm = ask | mid_down | bid | wait) is priced
against the consolidated SIP NBBO at the moment the desk decided to buy (exec_report.nbbo_at):
entry_test.t_decide, stamped before any arm waits. The wait arm's first buy is sent up to
ai_entry_test_cross_sec after that, so pricing it at its submit would drop the move during the
wait from its cost (review 2026-10-03). Outcomes from before t_decide existed use the first buy
submit, minus waited_sec for the wait arm.
The arms' own limits use the IEX quote, which overstates spreads, so the SIP mid is the fair yardstick.

Per trade, in bp:
  cost   entry fill / SIP mid at send - 1         what the entry paid (+ = paid)
  e2e    exit fill / SIP mid at send - 1          the whole trade from one common reference: a passive
                                                  arm that fills cheaper but only when price is falling
                                                  shows up here, not in cost
  trade  exit / entry - 1                         the desk's own P&L view
  passive  a limit buy for the trade filled (some shares rested rather than crossed)

The verdict line compares each passive arm with the ask (control) arm on e2e, day by day; the t is
across days (each day = mean(arm) - mean(ask)), so it means nothing until ~10 sessions.

USAGE (on the mini, after the close; SIP quotes are free once 15 minutes old)
    .venv/bin/python tools/entry_arm_score.py                       # every day with arm data
    .venv/bin/python tools/entry_arm_score.py --from 2026-10-02 --detail
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as xr  # noqa: E402
import tape_check as tc  # noqa: E402

ARMS = ("ask", "mid_down", "bid", "wait")
CACHE = os.path.join(ROOT, "ai_reports", "entry_arm_score_cache.json")
SEND_WINDOW = (-120.0, 5.0)   # first buy submit within this many seconds of the outcome's entry_time
CROSS_GAP = 30.0              # a passive arm's market cross follows its limit within this many seconds


def outcomes(lo: str, hi: str) -> list[dict]:
    out = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        et = r.get("entry_test") or {}
        if et.get("arm") not in ARMS or not r.get("entry_time") or not r.get("exit_price"):
            continue
        if (r.get("hold_days") or 0) > 0:
            continue
        day = bars.day_of(float(r["entry_time"]))
        if lo <= day <= hi:
            out.append({**r, "_day": day})
    return out


def ref_time(entry_test: dict, t_send: float) -> float:
    """When the arm's cost clock starts: the decision, not the (possibly delayed) first submit."""
    et = entry_test or {}
    try:
        if et.get("t_decide"):
            return float(et["t_decide"])
        if et.get("arm") == "wait":
            # Pre-t_decide outcomes: the wait arm's only submit came after the wait.
            return t_send - float(et.get("waited_sec") or 0.0)
    except (TypeError, ValueError):
        pass
    return t_send


def buys_by_symbol(day: str) -> dict[str, list[dict]]:
    """Buy submits (with their fill, if any) for the day, by symbol."""
    path = os.path.join(ROOT, "ai_reports", "fills", f"{day}.jsonl")
    subs, fills = {}, {}
    if not os.path.exists(path):
        return {}
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        oid = r.get("order_id")
        if r.get("event") == "submit" and str(r.get("action")).upper() == "BUY" and oid:
            subs[oid] = r
        elif r.get("event") == "fill" and oid:
            fills[oid] = r
    out = defaultdict(list)
    for oid, s in subs.items():
        f = fills.get(oid, {})
        out[s["symbol"]].append({"ts": float(s["ts"]), "type": f.get("type"),
                                 "filled_qty": float(f.get("filled_qty") or 0)})
    for v in out.values():
        v.sort(key=lambda x: x["ts"])
    return out


def tape_honest_entry(et: dict, sym: str, t: float, ep: float, quote, prints) -> tuple[str | None, float]:
    """(tape verdict, live-equivalent entry price) for one entry.

    Only a passive arm (bid / mid_down) whose limit bought shares on paper is checked: was there a SIP print
    below the limit while it rested? If not (or only a touch), the passive shares are re-priced at the SIP ask
    when the arm would have crossed (decision + waited_sec, else + ai_entry_test_cross_sec 10 s).
    """
    pq = float(et.get("passive_qty") or 0)
    lim = et.get("limit")
    if et.get("arm") not in ("bid", "mid_down") or pq <= 0 or not lim or prints is None:
        return None, ep
    wait = float(et.get("waited_sec") or 10.0)
    pr = prints(sym, t, t + wait)
    if pr is None:                       # tape unavailable: leave the paper price, unchecked
        return None, ep
    verdict = tc.tape_verdict("buy", float(lim), pr)
    if verdict == "confirmed":
        return verdict, ep
    h = tc.honest_price("buy", verdict, float(lim), quote(sym, t + wait))
    if h is None:
        return verdict, ep
    cq = float(et.get("crossed_qty") or 0)
    if cq > 0:
        # the crossed leg's own price, backed out of the blended fill
        fill = float(et.get("fill") or ep)
        cpx = (fill * (pq + cq) - pq * float(lim)) / cq
        return verdict, (pq * h + cq * cpx) / (pq + cq)
    return verdict, h


def dct(vals_by_day: dict[str, list[float]]):
    """Mean of all values, and t across day means."""
    allv = [x for v in vals_by_day.values() for x in v]
    dm = [statistics.mean(v) for v in vals_by_day.values() if v]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else None)
    return (statistics.mean(allv) if allv else None), t, len(allv), len(dm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="lo", default="2026-10-01")
    ap.add_argument("--to", dest="hi", default=datetime.now(bars.ET).strftime("%Y-%m-%d"))
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cl = [None]

    def quote(sym, t):
        key = f"{sym}|{t:.3f}"
        if key not in cache:
            cl[0] = cl[0] or bars.client()
            q = xr.nbbo_at(cl[0], sym, datetime.fromtimestamp(t, timezone.utc))
            time.sleep(0.3)   # the live engine shares these data keys
            cache[key] = list(q) if q else None
        return cache[key]

    def prints(sym, t0, t1):
        key = f"P|{sym}|{t0:.3f}|{t1:.3f}"
        if key not in cache:
            cl[0] = cl[0] or bars.client()
            try:
                cache[key] = tc.regular_prints(cl[0], sym, t0, t1)
            except Exception as e:  # noqa: BLE001
                print(f"  prints fail {sym}: {str(e)[:60]}", file=sys.stderr)
                return None
            time.sleep(0.3)
        return cache[key]

    rows, buys, used = [], {}, set()
    for r in sorted(outcomes(args.lo, args.hi), key=lambda r: float(r["entry_time"])):
        sym, day, t_entry = r["symbol"], r["_day"], float(r["entry_time"])
        if day not in buys:
            buys[day] = buys_by_symbol(day)
        # Each buy order belongs to one trade: take the first unused submit in the window, plus the
        # passive arm's cross leg (another submit within CROSS_GAP seconds of it).
        near = [b for b in buys[day].get(sym, [])
                if SEND_WINDOW[0] <= b["ts"] - t_entry <= SEND_WINDOW[1] and (sym, b["ts"]) not in used]
        if near:
            near = [b for b in near if b["ts"] - near[0]["ts"] <= CROSS_GAP]
            used.update((sym, b["ts"]) for b in near)
        t_send = ref_time(r["entry_test"], near[0]["ts"] if near else t_entry)
        q = quote(sym, t_send)
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        ep, xp = float(r["entry_price"]), float(r["exit_price"])
        tape, hep = tape_honest_entry(r["entry_test"], sym, t_send, ep, quote, prints)
        rows.append({"day": day, "sym": sym, "arm": r["entry_test"]["arm"], "t": t_send,
                     "tape": tape, "net": (xp - ep) / mid * 1e4, "honest_net": (xp - hep) / mid * 1e4,
                     "matched": bool(near),
                     "passive": any(b["type"] == "limit" and b["filled_qty"] > 0 for b in near),
                     "half": (ask - bid) / 2 / mid * 1e4,
                     "cost": (ep / mid - 1) * 1e4, "e2e": (xp / mid - 1) * 1e4,
                     "trade": (xp / ep - 1) * 1e4})
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(cache, open(CACHE, "w"))

    days = sorted({x["day"] for x in rows})
    print(f"ENTRY ARM SCORE {args.lo}..{args.hi}  ({len(rows)} trades, {len(days)} day(s); bp vs SIP mid at decision)\n")
    if not rows:
        return
    unmatched = sum(1 for x in rows if not x["matched"])
    if unmatched:
        print(f"  note: {unmatched} trade(s) had no buy submit near entry_time; priced at entry_time instead\n")
    print(f"  {'arm':<10}{'n':>5}{'passive%':>10}{'tape ok':>9}{'half spr':>10}{'cost':>9}{'e2e':>9}{'net':>9}"
          f"{'net - ask':>12}{'t(days)':>9}{'honest':>9}{'hon - ask':>11}{'t(days)':>9}")
    by = defaultdict(lambda: defaultdict(list))
    for x in rows:
        by[x["arm"]][x["day"]].append(x)
    ask_day = {d: statistics.mean(v["net"] for v in xs) for d, xs in by["ask"].items()}
    ask_hon = {d: statistics.mean(v["honest_net"] for v in xs) for d, xs in by["ask"].items()}
    for arm in ARMS:
        xs = [x for v in by[arm].values() for x in v]
        if not xs:
            continue
        m = lambda k: statistics.mean(x[k] for x in xs)
        diff = {d: [statistics.mean(x["net"] for x in v) - ask_day[d]]
                for d, v in by[arm].items() if d in ask_day} if arm != "ask" else {}
        dm, dt, _, nd = dct(diff) if diff else (None, None, 0, 0)
        hdiff = {d: [statistics.mean(x["honest_net"] for x in v) - ask_hon[d]]
                 for d, v in by[arm].items() if d in ask_hon} if arm != "ask" else {}
        hm, ht, _, _ = dct(hdiff) if hdiff else (None, None, 0, 0)
        checked = [x for x in xs if x["tape"] is not None]
        ok = (f"{100 * sum(x['tape'] == 'confirmed' for x in checked) / len(checked):>8.0f}%"
              if checked else f"{'—':>9}")
        print(f"  {arm:<10}{len(xs):>5}{100 * sum(x['passive'] for x in xs) / len(xs):>9.0f}%{ok}"
              f"{m('half'):>10.1f}{m('cost'):>+9.1f}{m('e2e'):>+9.1f}{m('net'):>+9.1f}"
              + (f"{dm:>+12.1f}{(f'{dt:+.2f}' if dt is not None else '—'):>9}" if dm is not None else f"{'—':>12}{'—':>9}")
              + f"{m('honest_net'):>+9.1f}"
              + (f"{hm:>+11.1f}{(f'{ht:+.2f}' if ht is not None else '—'):>9}" if hm is not None else f"{'—':>11}{'—':>9}"))
    print("\n  net = (exit - entry) / SIP mid at decision: the whole trade from one reference (e2e alone leaves out the"
          "\n  entry price, which is what the arms change). net - ask is the mean of daily (arm - ask) differences."
          "\n  tape ok = share of paper passive fills with a SIP print below the limit while it rested; honest = net"
          "\n  with unconfirmed passive fills re-priced at the ask when the arm crosses. t needs ~10 days.")
    if args.detail:
        print(f"\n  {'day':<11}{'time':<9}{'sym':<7}{'arm':<10}{'pass':>5}{'half':>7}{'cost':>8}{'e2e':>8}{'trade':>8}")
        for x in sorted(rows, key=lambda x: x["t"]):
            tt = datetime.fromtimestamp(x["t"], bars.ET).strftime("%H:%M:%S")
            print(f"  {x['day']:<11}{tt:<9}{x['sym']:<7}{x['arm']:<10}{'y' if x['passive'] else '':>5}"
                  f"{x['half']:>7.1f}{x['cost']:>+8.1f}{x['e2e']:>+8.1f}{x['trade']:>+8.1f}")


if __name__ == "__main__":
    main()
