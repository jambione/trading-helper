#!/usr/bin/env python3
"""One end-of-day report, with the number the paper account does not show.

Paper flatters. Alpaca fills entries honestly at the ask but lets exits go a
penny under the trigger print, where a real market sell crosses all the way to
the bid — four to nine cents on these books. Measured 2026-08-21: paper charges
about a quarter of the true exit cost, so every P&L this desk has ever printed
is optimistic by roughly 0.09R per trade.

Which matters most at exactly the moment it is easiest to forget: deciding
whether a good-looking paper fortnight justifies a live account. So the
live-equivalent number is a column here rather than a correction someone has
to remember to apply.

    live-equivalent R = realized R − (half the round trip − what paper charged)

Both halves are measured per trade, not assumed: half the round trip from the
spread recorded on the fill, and what paper charged from the gap between the
exit trigger print and the actual exit fill.

THE DECISION METRIC is `MFE − spread`: the trade's best moment minus what it
cost to be there. Above zero the trade was worth taking before any exit
question; below it, nothing downstream can help. On 2026-08-21 the median was
−0.038R and only 5 of 22 cleared their own round trip.

Read-only. Usage:
    python3 tools/eod.py                 # today, plus the running ledger
    python3 tools/eod.py --days 10
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
EVENTS = os.path.join(ROOT, "ai_reports", "events.jsonl")

# Go-live bar. Session-level on purpose: pooled numbers let one good day carry
# the decision, which is the mistake this desk has already made once.
GO_LIVE_SESSIONS = 10
GO_LIVE_WIN_SESSIONS = 7
GO_LIVE_MIN_TRADES = 30


def _jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def exit_slip_by_trade(days: set[str]) -> dict:
    """(symbol, exit_ts) -> trigger print, from the trail exit events.

    What paper actually charged on the way out is the gap between the print
    that tripped the shelf and the price the position closed at. Measured
    rather than assumed, because it is the whole basis of the correction.
    """
    out = defaultdict(list)
    for d in _jsonl(EVENTS):
        ts = d.get("ts")
        if not ts or bars.day_of(ts) not in days:
            continue
        if d.get("kind") != "local_trail":
            continue
        if d.get("last") is not None:
            out[str(d.get("symbol") or "").upper()].append(
                (float(ts), float(d["last"])))
    return out


def load_sessions(days_back: int):
    rows = defaultdict(list)
    for d in _jsonl(OUTCOMES):
        ts = d.get("ts")
        if not ts:
            continue
        rows[bars.day_of(ts)].append(d)
    days = sorted(rows)[-days_back:]
    return days, {k: rows[k] for k in days}


def score_session(trades, trig):
    """Per-session totals, including the cost paper never charged."""
    paper_r = paper_usd = 0.0
    live_r = 0.0
    unpaid_all, mfe_less_spread, per_r = [], [], []
    n_spread = 0
    for d in trades:
        r = d.get("realized_r_multiple")
        u = d.get("realized_pl_usd")
        if r is None:
            continue
        r = float(r)
        paper_r += r
        if u is not None:
            paper_usd += float(u)
            if r:
                per_r.append(float(u) / r)
        f = d.get("features") or {}
        sp = f.get("spread_r")
        e, st = d.get("entry_price"), d.get("stop_price")
        unpaid = 0.0
        if sp is not None and e and st:
            risk = float(e) - float(st)
            if risk > 0:
                n_spread += 1
                half = float(sp) / 2.0                 # one side, in R
                # what paper charged: trigger print minus the exit fill
                charged = 0.0
                xp, xt = d.get("exit_price"), d.get("exit_time")
                cands = trig.get(str(d.get("symbol") or "").upper()) or []
                if xp and xt and cands:
                    _ts, last = min(cands, key=lambda c: abs(c[0] - float(xt)))
                    charged = max(0.0, (last - float(xp)) / risk)
                unpaid = max(0.0, half - charged)
                unpaid_all.append(unpaid)
                m = d.get("mfe_r")
                if m is not None:
                    mfe_less_spread.append(float(m) - float(sp))
        live_r += r - unpaid
    dollars_per_r = statistics.median(per_r) if per_r else 0.0
    return {
        "n": len(trades), "n_spread": n_spread,
        "paper_r": paper_r, "paper_usd": paper_usd,
        "live_r": live_r, "live_usd": live_r * dollars_per_r,
        "unpaid": statistics.median(unpaid_all) if unpaid_all else None,
        "mfe_less_spread": (statistics.median(mfe_less_spread)
                            if mfe_less_spread else None),
    }


def spread_health(days: set[str]):
    ev = sp = 0
    vals = []
    for r in sr.load():
        ts = r.get("ts")
        if not ts or bars.day_of(ts) not in days:
            continue
        if r.get("arm_ok") is None:
            continue
        if not (9 * 60 + 30 <= bars.et_minutes(ts) < 16 * 60):
            continue
        ev += 1
        v = r.get("spread_r")
        if v is not None:
            sp += 1
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    return ev, sp, (statistics.median(vals) if vals else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    days, by_day = load_sessions(args.days)
    if not days:
        print("no outcome rows")
        return 1
    trig = exit_slip_by_trade(set(days))
    scored = {d: score_session(by_day[d], trig) for d in days}
    today = days[-1]
    t = scored[today]

    print("=" * 66)
    print(f"  EOD — {today}")
    print("=" * 66)
    try:
        from config import load_config
        import desk_product
        cfg = load_config()
        print(f"  desk_product={desk_product.product(cfg)}  "
              f"h4_paper={desk_product.h4_paper(cfg)}")
    except Exception:
        pass
    print(f"\n  {t['n']} trades closed")
    print(f"  paper            {t['paper_r']:+.3f} R   ${t['paper_usd']:+.2f}")
    print(f"  LIVE-EQUIVALENT  {t['live_r']:+.3f} R   ${t['live_usd']:+.2f}"
          "   <- what a real broker would have paid you")
    if t["unpaid"] is not None:
        print(f"  paper under-charged the exit by {t['unpaid']:.3f}R per trade "
              f"(median, measured)")

    print("\n  DECISION METRIC — the trade's best moment minus what it cost")
    if t["mfe_less_spread"] is not None:
        v = t["mfe_less_spread"]
        print(f"  median MFE - spread   {v:+.3f} R   "
              f"{'ABOVE water' if v > 0 else 'still underwater'}")
        print("  Above zero the trade was worth taking before any exit")
        print("  question. Below it, no exit setting can rescue it.")
    else:
        print("  no spread readings on today's fills — cannot say")

    ev, spn, med = spread_health({today})
    print(f"\n  crossing-cost record: {spn}/{ev} arm-evaluated rows "
          f"({100*spn/max(1,ev):.0f}%)"
          + (f", RTH median spread {med:.3f}R" if med is not None else ""))

    print("\n" + "-" * 66)
    print(f"  {'session':<12} {'n':>4} {'paper R':>9} {'LIVE R':>9} "
          f"{'live $':>9} {'MFE-spr':>9}")
    print("-" * 66)
    for d in days:
        s = scored[d]
        mfe = f"{s['mfe_less_spread']:+.3f}" if s["mfe_less_spread"] is not None else "—"
        print(f"  {d:<12} {s['n']:>4} {s['paper_r']:>+9.3f} {s['live_r']:>+9.3f} "
              f"{s['live_usd']:>+9.2f} {mfe:>9}")

    # Go-live readout. Deliberately session-level: pooling lets one good day
    # carry a decision about real money.
    wins = sum(1 for d in days if scored[d]["live_r"] > 0)
    trades = sum(scored[d]["n"] for d in days)
    print("\n" + "=" * 66)
    print("  GO-LIVE CHECK (live-equivalent, session-level)")
    print("=" * 66)
    ok_s = wins >= GO_LIVE_WIN_SESSIONS and len(days) >= GO_LIVE_SESSIONS
    ok_n = trades >= GO_LIVE_MIN_TRADES
    mfes = [scored[d]["mfe_less_spread"] for d in days
            if scored[d]["mfe_less_spread"] is not None]
    ok_m = bool(mfes) and statistics.median(mfes) > 0
    print(f"  [{'x' if ok_s else ' '}] {wins}/{len(days)} sessions live-positive "
          f"(need {GO_LIVE_WIN_SESSIONS} of {GO_LIVE_SESSIONS})")
    print(f"  [{'x' if ok_n else ' '}] {trades} trades (need {GO_LIVE_MIN_TRADES})")
    print(f"  [{'x' if ok_m else ' '}] median MFE-spread "
          + (f"{statistics.median(mfes):+.3f}R" if mfes else "—") + " (need > 0)")
    print()
    if ok_s and ok_n and ok_m:
        print("  All three met. Live is a defensible next step.")
    else:
        print("  NOT met. A paper fortnight that looks fine is not evidence:")
        print("  paper does not charge the exit, which is the whole point of")
        print("  the LIVE R column above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
