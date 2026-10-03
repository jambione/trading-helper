#!/usr/bin/env python3
"""exit_arm_score.py — grade the exit-cost A/B (ai_exit_test_arms) against the SIP mid.

Each closed day trade whose outcome carries ``exit_test`` (arm = market | mid) is priced against
the consolidated SIP NBBO at the trail hit (exit_test.t_decide, stamped before either arm sends;
older outcomes fall back to the first SELL submit near exit_time). The mid arm's own limit uses the
IEX quote, which overstates spreads, so the SIP mid is the fair yardstick. Sibling of
entry_arm_score.py (review 2026-10-03: the exit arm shipped with no scorer).

Per trade, in bp:
  cost   1 - exit fill / SIP mid at decision     what the exit gave up (+ = paid)
  half   SIP half-spread at decision            what a market sell at the bid would pay
  passive%  the mid arm's limit sold every share (crossed_qty 0)

Intention to treat: a mid-arm exit that fell back to market (no quote), crossed, or was
superseded by another exit (stop, EOD) still counts as the mid arm — the arm is what was drawn.
The exit fill is the outcome's exit_price, which is exit_test.fill (both legs blended) when set.

The verdict compares mid with market day by day; the t is across days (each day = mean(mid) -
mean(market)), so it means nothing until ~10 sessions.

USAGE (on the mini, after the close; SIP quotes are free once 15 minutes old)
    .venv/bin/python tools/exit_arm_score.py
    .venv/bin/python tools/exit_arm_score.py --from 2026-10-05 --detail
"""
from __future__ import annotations

import argparse
import json
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
from entry_arm_score import dct  # noqa: E402
import tape_check as tc  # noqa: E402

ARMS = ("market", "mid")
CACHE = os.path.join(ROOT, "ai_reports", "exit_arm_score_cache.json")
SEND_WINDOW = (-120.0, 5.0)   # first sell submit within this many seconds of the outcome's exit_time
CROSS_SEC = 10.0              # ai_exit_test_cross_sec default: when an unfilled limit would have crossed
BOOK_TICK_SEC = 2.0           # the settle runs on the book tick, so a live rest lasts up to one tick longer


def outcomes(lo: str, hi: str) -> list[dict]:
    out = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        xt = r.get("exit_test") or {}
        if xt.get("arm") not in ARMS or not r.get("exit_time") or not r.get("exit_price"):
            continue
        if (r.get("hold_days") or 0) > 0:
            continue
        day = bars.day_of(float(r["exit_time"]))
        if lo <= day <= hi:
            out.append({**r, "_day": day})
    return out


def sell_submits(day: str) -> dict[str, list[float]]:
    """SELL submit times for the day, by symbol."""
    path = os.path.join(ROOT, "ai_reports", "fills", f"{day}.jsonl")
    out = defaultdict(list)
    if not os.path.exists(path):
        return {}
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if (r.get("event") == "submit" and str(r.get("action") or "").upper().startswith("SELL")
                and r.get("order_id") and r.get("symbol")):
            out[str(r["symbol"]).upper()].append(float(r["ts"]))
    for v in out.values():
        v.sort()
    return out


def ref_time(r: dict, sells: dict[str, list[float]]) -> tuple[float, bool]:
    """(decision time, matched) for one outcome."""
    xt = r.get("exit_test") or {}
    try:
        if xt.get("t_decide"):
            return float(xt["t_decide"]), True
    except (TypeError, ValueError):
        pass
    t_exit = float(r["exit_time"])
    near = [t for t in sells.get(str(r["symbol"]).upper(), [])
            if SEND_WINDOW[0] <= t - t_exit <= SEND_WINDOW[1]]
    return (near[0], True) if near else (t_exit, False)


def tape_honest_exit(r: dict, t: float, xp: float, quote, prints) -> tuple[str | None, float | None]:
    """(tape status, live-equivalent exit price) for one outcome.

    Only a mid-arm exit whose limit sold shares on paper is checked: did SIP round lots print above the limit,
    totalling our passive shares, while a live order would have rested there? The rest window is the arm's whole
    wait (cross time plus one book tick) for a full paper fill — paper filling at 1.5 s says nothing about a live
    queue — and waited_sec when the limit really timed out and crossed. If not confirmed (a touch or nothing), the
    passive shares are re-priced at the SIP bid at the end of that window; the crossed leg keeps its own price.
    Status None = nothing to check (market arm, nothing filled passively); "unchecked" = could not be judged
    (superseded by another exit, tape or deadline quote unavailable, crossed price unknown) and the price is None.
    """
    xt = r.get("exit_test") or {}
    pq = float(xt.get("passive_qty") or 0)
    lim = xt.get("limit")
    if xt.get("arm") != "mid" or pq <= 0 or not lim:
        return None, xp
    if xt.get("superseded") or prints is None:
        return "unchecked", None
    cq = float(xt.get("crossed_qty") or 0)
    rest = CROSS_SEC + BOOK_TICK_SEC if cq <= 0 else float(xt.get("waited_sec") or CROSS_SEC)
    pr = prints(r["symbol"], t, t + rest)
    if pr is None:
        return "unchecked", None
    verdict = tc.tape_verdict("sell", float(lim), pr, pq)
    if verdict == "confirmed":
        return verdict, xp
    h = tc.honest_price("sell", verdict, float(lim), quote(r["symbol"], t + rest))
    if h is None:
        return "unchecked", None
    if cq > 0:
        cpx = xt.get("crossed_px")
        if not cpx:
            return "unchecked", None
        return verdict, (pq * h + cq * float(cpx)) / (pq + cq)
    return verdict, h


def score(rows_in: list[dict], quote, prints=None) -> list[dict]:
    """Per-trade rows; quote(sym, t) -> (bid, ask) or None; prints(sym, t0, t1) -> [(t, px)] or None."""
    rows, sells = [], {}
    for r in sorted(rows_in, key=lambda r: float(r["exit_time"])):
        day = r["_day"]
        if day not in sells:
            sells[day] = sell_submits(day)
        t, matched = ref_time(r, sells[day])
        q = quote(r["symbol"], t)
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        xt = r["exit_test"]
        xp = float(r["exit_price"])
        tape, hx = tape_honest_exit(r, t, xp, quote, prints)
        rows.append({"day": day, "sym": r["symbol"], "arm": xt["arm"], "t": t, "matched": matched,
                     "tape": tape, "honest_cost": None if hx is None else (1 - hx / mid) * 1e4,
                     "passive": bool(xt.get("arm") == "mid" and xt.get("fill") is not None
                                     and not xt.get("crossed_qty")),
                     "fallback": bool(xt.get("fallback")), "superseded": bool(xt.get("superseded")),
                     "half": (ask - bid) / 2 / mid * 1e4, "cost": (1 - xp / mid) * 1e4})
    return rows


def report(rows: list[dict], lo: str, hi: str, detail: bool = False) -> None:
    days = sorted({x["day"] for x in rows})
    print(f"EXIT ARM SCORE {lo}..{hi}  ({len(rows)} trades, {len(days)} day(s); bp vs SIP mid at the trail hit)\n")
    if not rows:
        return
    unmatched = sum(1 for x in rows if not x["matched"])
    if unmatched:
        print(f"  note: {unmatched} trade(s) had no t_decide and no sell submit near exit_time; priced at exit_time\n")
    print(f"  {'arm':<8}{'n':>5}{'passive%':>10}{'tape ok/chk u':>14}{'fallbk':>8}{'supersd':>9}{'half spr':>10}{'cost':>9}"
          f"{'cost - mkt':>12}{'t(days)':>9}{'honest':>9}{'hon - mkt':>11}{'t(days)':>9}")
    by = defaultdict(lambda: defaultdict(list))
    for x in rows:
        by[x["arm"]][x["day"]].append(x)
    mkt_day = {d: statistics.mean(v["cost"] for v in xs) for d, xs in by["market"].items()}
    hmean = lambda xs: (statistics.mean(v) if (v := [x["honest_cost"] for x in xs if x["honest_cost"] is not None])
                        else None)
    mkt_hon = {d: hmean(xs) for d, xs in by["market"].items() if hmean(xs) is not None}
    for arm in ARMS:
        xs = [x for v in by[arm].values() for x in v]
        if not xs:
            continue
        diff = {d: [statistics.mean(x["cost"] for x in v) - mkt_day[d]]
                for d, v in by[arm].items() if d in mkt_day} if arm != "market" else {}
        dm, dt, _, _ = dct(diff) if diff else (None, None, 0, 0)
        hdiff = {d: [hmean(v) - mkt_hon[d]]
                 for d, v in by[arm].items() if d in mkt_hon and hmean(v) is not None} if arm != "market" else {}
        hm, ht, _, _ = dct(hdiff) if hdiff else (None, None, 0, 0)
        checked = [x for x in xs if x["tape"] not in (None, "unchecked")]
        unchecked = sum(1 for x in xs if x["tape"] == "unchecked")
        ok = (f"{sum(x['tape'] == 'confirmed' for x in checked):>4}/{len(checked):<3}u{unchecked:<2}"
              if checked or unchecked else f"{'—':>11}")
        fmt = lambda m_, t_: (f"{m_:>+12.1f}{(f'{t_:+.2f}' if t_ is not None else '—'):>9}" if m_ is not None
                              else f"{'—':>12}{'—':>9}")
        print(f"  {arm:<8}{len(xs):>5}{100 * sum(x['passive'] for x in xs) / len(xs):>9.0f}%{ok}"
              f"{sum(x['fallback'] for x in xs):>8}{sum(x['superseded'] for x in xs):>9}"
              f"{statistics.mean(x['half'] for x in xs):>10.1f}{statistics.mean(x['cost'] for x in xs):>+9.1f}"
              + fmt(dm, dt) + (f"{hmean(xs):>+9.1f}" if hmean(xs) is not None else f"{'—':>9}")
              + (f"{hm:>+11.1f}{(f'{ht:+.2f}' if ht is not None else '—'):>9}" if hm is not None else f"{'—':>11}{'—':>9}"))
    print("\n  cost - mkt is the mean of daily (mid - market) cost differences; negative = mid saved."
          "\n  passive% = limit sold every share; the tape check also covers partial passive fills."
          "\n  tape ok/chk u = passive paper fills confirmed by SIP round lots printing above the limit (our size) while"
          "\n  a live order would have rested / fills checked, u = unchecked (superseded, tape or prices unavailable;"
          "\n  left out of honest). honest = cost with unconfirmed passive shares re-priced at the SIP bid when the arm"
          "\n  would have crossed; the market control's cost is a paper market fill."
          "\n  t needs ~10 days to mean anything.")
    if detail:
        print(f"\n  {'day':<11}{'time':<9}{'sym':<7}{'arm':<8}{'pass':>5}{'half':>7}{'cost':>8}{'honest':>8}{'tape':>11}")
        for x in sorted(rows, key=lambda x: x["t"]):
            tt = datetime.fromtimestamp(x["t"], bars.ET).strftime("%H:%M:%S")
            print(f"  {x['day']:<11}{tt:<9}{x['sym']:<7}{x['arm']:<8}{'y' if x['passive'] else '':>5}"
                  f"{x['half']:>7.1f}{x['cost']:>+8.1f}"
                  + (f"{x['honest_cost']:>+8.1f}" if x['honest_cost'] is not None else f"{'—':>8}")
                  + f"{(x['tape'] or ''):>11}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="lo", default="2026-10-05")
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
            if not q:
                return None          # not cached: a 429 or a gap must not stick as "no quote" forever
            cache[key] = list(q)
        return cache[key]

    def prints(sym, t0, t1):
        key = f"P2|{sym}|{t0:.3f}|{t1:.3f}"   # P2: (t, px, size); P| rows had no size
        if key not in cache:
            cl[0] = cl[0] or bars.client()
            try:
                cache[key] = tc.regular_prints(cl[0], sym, t0, t1)
            except Exception as e:  # noqa: BLE001
                print(f"  prints fail {sym}: {str(e)[:60]}", file=sys.stderr)
                time.sleep(2.0)      # back off: a burst of 429s must not cascade
                return None
            time.sleep(0.3)
        return cache[key]

    rows = score(outcomes(args.lo, args.hi), quote, prints)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(cache, open(CACHE, "w"))
    report(rows, args.lo, args.hi, args.detail)


if __name__ == "__main__":
    main()
