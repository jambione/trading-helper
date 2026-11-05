#!/usr/bin/env python3
"""Trend-hold scorecard (operator 11/05, after EQNR): did holding an at-entry trail sale pay?

For every position that had at least one `trail_trend_hold` event, compare the ACTUAL exit price with what a sale at the
FIRST trail trigger would have fetched (the last print on the first hold event; no slippage on either side).
  delta bp = (actual exit - first-trigger price) / entry x 1e4     (> 0: the hold helped)
Reads ai_reports/events.jsonl (hold / decline events) and ai_reports/outcomes.jsonl (closed trades). Positions still open
are listed but not scored. Prints one line per held trade and a summary; --since YYYY-MM-DD limits by exit day (ET).
After ~20 held trades the summary is the evidence for keeping or revisiting ai_trail_exit_trend_hold.
usage: tools/hold_scorecard.py [--since 2026-11-05]"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")


def _rows(path):
    try:
        for line in open(path):
            try:
                yield json.loads(line)
            except ValueError:
                continue
    except OSError:
        return


def score(since: str | None = None, events_path=None, outcomes_path=None):
    events_path = events_path or os.path.join(ROOT, "ai_reports/events.jsonl")
    outcomes_path = outcomes_path or os.path.join(ROOT, "ai_reports/outcomes.jsonl")
    holds, declines = {}, {}
    for e in _rows(events_path):
        k, s = e.get("kind"), str(e.get("symbol") or "")
        if k == "trail_trend_hold":
            holds.setdefault(s, []).append(e)
        elif k == "trail_hold_declined":
            declines.setdefault(s, []).append(e)
    out, open_names = [], []
    seen = set()
    for o in _rows(outcomes_path):
        s = str(o.get("symbol") or "")
        et, xt, ep, xp = o.get("entry_time"), o.get("exit_time"), o.get("entry_price"), o.get("exit_price")
        if not (s in holds and et and xt and ep and xp) or (s, round(et)) in seen:
            continue
        seen.add((s, round(et)))
        day = datetime.fromtimestamp(xt, ET).strftime("%Y-%m-%d")
        if since and day < since:
            continue
        hs = sorted((h for h in holds[s] if et <= h["ts"] <= xt + 5), key=lambda h: h["ts"])
        if not hs:
            continue
        first = float(hs[0]["last"])
        qty = float(o.get("total_qty") or 0)
        decl = [d for d in declines.get(s, []) if et <= d["ts"] <= xt + 5]
        out.append({"day": day, "symbol": s, "entry": ep, "first_trigger": first, "exit": xp, "holds": len(hs),
                    "ended_by": (decl[-1].get("reason") if decl else str(o.get("close_reason") or "?")),
                    "delta_bp": (xp - first) / ep * 1e4, "delta_usd": (xp - first) * qty,
                    "hold_sec": xt - hs[0]["ts"]})
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since")
    rows = score(ap.parse_args().since)
    if not rows:
        print("no held trades with a recorded exit yet"); return 0
    for r in rows:
        print(f"{r['day']} {r['symbol']:5s} entry {r['entry']:.2f} first trigger {r['first_trigger']:.3f} exit {r['exit']:.3f} "
              f"holds {r['holds']} held {r['hold_sec']:.0f}s {r['delta_bp']:+.1f} bp ${r['delta_usd']:+.2f}  ended: {r['ended_by']}")
    d = [r["delta_bp"] for r in rows]
    se = st.pstdev(d) / math.sqrt(len(d)) if len(d) > 2 else float("nan")
    print(f"\nHELD TRADES {len(rows)}: mean {sum(d)/len(d):+.1f} bp vs selling at the first trigger (SE {se:.1f}), "
          f"total ${sum(r['delta_usd'] for r in rows):+.2f}, helped {sum(x > 0 for x in d)} / hurt {sum(x < 0 for x in d)}")
    if len(rows) < 20:
        print("fewer than 20 held trades: not enough to judge the hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
