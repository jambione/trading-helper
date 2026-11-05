#!/usr/bin/env python3
"""Daily scoreboard (operator 11/05): one line per session and a rolling-20 summary from the desk's own outcomes.
Reads ai_reports/tapes/<day>/outcomes.jsonl (single-day folders), de-duplicated by (symbol, round(entry_time)).
Net bp/trade = (exit-entry)/entry - 3 bp cost. Never-green = peak_price < entry +5 bp. Writes docs/SCOREBOARD.md.
Regression guard (thresholds fixed 2026-11-05, do not retune after a bad week): compares the last 10 sessions with the 10
before them on day-level mean net bp/trade (Welch t). GROWING: diff >= +2 bp and t >= 1.5. REGRESSING: diff <= -2 bp and t <= -1.5.
Otherwise FLAT. docs/ship_log.json lists each shipped change with its date; the board shows net bp/trade before vs after each ship
(NOT ENOUGH DATA under 10 sessions after).
usage: tools/daily_scoreboard.py [--days 20]"""
from __future__ import annotations

import argparse
import datetime
import glob
from zoneinfo import ZoneInfo
import json
import math
import os
import re
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COST_BP = 3.0
ET = ZoneInfo("America/New_York")


def load_day(path):
    rows, seen = [], set()
    for line in open(path):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        sym = str(o.get("symbol") or "")
        if not (isinstance(e, (int, float)) and e > 0 and x and t) or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        k = (sym, round(t))
        if k in seen:
            continue
        seen.add(k)
        pk = o.get("peak_price") or e
        risk = abs(e - float(o.get("stop_price") or 0.0))      # 1R in dollars per share (the synthetic stop distance)
        mfe_r, mae_r = o.get("mfe_r"), o.get("mae_r")
        hr = datetime.datetime.fromtimestamp(t, ET).hour
        rows.append({"bp": (x - e) / e * 1e4 - COST_BP, "usd": float(o.get("realized_pl_usd") or 0.0),
                     "ng": (pk - e) / e * 1e4 < 5, "hr": hr,
                     "mfe_bp": (max(0.0, float(mfe_r)) * risk / e * 1e4) if (mfe_r is not None and risk > 0) else None,
                     "mae_bp": (abs(min(0.0, float(mae_r))) * risk / e * 1e4) if (mae_r is not None and risk > 0) else None})
    return rows


def payoff_block(allr):
    """Operator 11/05: profit = win rate x avg win - loss rate x avg loss. Shows how far the desk is from breakeven."""
    w = [x["bp"] for x in allr if x["bp"] > 0]
    l = [x["bp"] for x in allr if x["bp"] <= 0]
    if not w or not l:
        return []
    aw, al = sum(w) / len(w), abs(sum(l) / len(l))
    wr = len(w) / len(allr)
    be_win = al / (al + aw)
    be_loss = wr * aw / (1 - wr) if wr < 1 else float("inf")
    ng = [x for x in allr if x["ng"]]
    loss_total = sum(x["bp"] for x in allr if x["bp"] < 0)
    ng_share = sum(x["bp"] for x in ng if x["bp"] < 0) / loss_total if loss_total else 0.0
    rest = [x["bp"] for x in allr if not x["ng"]]
    return ["## Profit arithmetic (profit needs a high win rate AND small losses)", "",
            f"- win rate **{wr:.0%}**, average win **{aw:+.1f} bp**, average loss **{-al:+.1f} bp**",
            f"- breakeven win rate at this payoff: **{be_win:.0%}** (gap {100*(be_win-wr):+.0f} points)",
            f"- breakeven average loss at this win rate: **-{be_loss:.1f} bp** (losses are {al/be_loss:.1f}x too big)",
            f"- never-green trades: {len(ng)} of {len(allr)}, average {sum(x['bp'] for x in ng)/max(1,len(ng)):+.1f} bp, "
            f"**{ng_share:.0%} of all loss**; the other trades average {sum(rest)/max(1,len(rest)):+.1f} bp", ""]


def edge_block(allr):
    """MFE / MAE edge ratio by ENTRY hour (ET), over the rolling window. Mean best excursion over mean worst excursion
    during the hold, in bp of the entry price. Above 1 = trades move our way before they move against us on average;
    ~1 = coin flip; the sources suggest > 2.0 is a strong entry edge (vendor rule of thumb, unvalidated). Hold-length
    dependent (a longer hold sees a bigger MFE and MAE); this is the whole hold, not fixed horizons."""
    by = {}
    for x in allr:
        if x.get("mfe_bp") is not None and x.get("mae_bp") is not None:
            by.setdefault(x["hr"], []).append(x)
    if not by:
        return []
    out = ["## Entry quality by hour (MFE / MAE edge ratio)", "",
           "| entry hour (ET) | trades | mean MFE bp | mean MAE bp | edge ratio | win % | net bp |", "|---|---:|---:|---:|---:|---:|---:|"]
    tot = []
    for hr in sorted(by):
        v = by[hr]; tot += v
        mf = sum(x["mfe_bp"] for x in v) / len(v); ma = sum(x["mae_bp"] for x in v) / len(v)
        out.append(f"| {hr:02d}:00 | {len(v)} | {mf:.1f} | {ma:.1f} | {mf/ma if ma > 0 else float('nan'):.2f} | "
                   f"{100*sum(x['bp']>0 for x in v)/len(v):.0f} | {sum(x['bp'] for x in v)/len(v):+.1f} |")
    mf = sum(x["mfe_bp"] for x in tot) / len(tot); ma = sum(x["mae_bp"] for x in tot) / len(tot)
    out += [f"| **all** | {len(tot)} | {mf:.1f} | {ma:.1f} | **{mf/ma if ma > 0 else float('nan'):.2f}** | "
            f"{100*sum(x['bp']>0 for x in tot)/len(tot):.0f} | {sum(x['bp'] for x in tot)/len(tot):+.1f} |", "",
            "Hours with fewer than ~30 trades are noise. Read the trend over weeks, not one night.", ""]
    return out


def _day_means(chunk):
    return [sum(x["bp"] for x in r) / len(r) for _, r in chunk]


def _welch(a, b):
    if len(a) < 3 or len(b) < 3:
        return float("nan"), float("nan")
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va, vb = st.variance(a) / len(a), st.variance(b) / len(b)
    se = math.sqrt(va + vb)
    return ma - mb, ((ma - mb) / se if se else float("nan"))


def trend_and_ships(days_all):
    out = ["## Is the project growing or regressing?", ""]
    if len(days_all) >= 20:
        diff, t = _welch(_day_means(days_all[-10:]), _day_means(days_all[-20:-10]))
        verdict = "GROWING" if diff >= 2 and t >= 1.5 else "REGRESSING" if diff <= -2 and t <= -1.5 else "FLAT"
        out.append(f"**{verdict}**: last 10 sessions vs the 10 before: {diff:+.1f} bp/trade (t {t:+.2f}). "
                   "Rule: GROWING needs >= +2 bp and t >= 1.5; REGRESSING <= -2 bp and t <= -1.5; anything else FLAT.")
    else:
        out.append("Fewer than 20 sessions: no verdict.")
    out.append("")
    try:
        ships = json.load(open(os.path.join(ROOT, "docs/ship_log.json")))
    except (OSError, ValueError):
        ships = []
    if ships:
        out += ["| shipped | change | sessions after | net bp before (10) | net bp after | read |", "|---|---|---:|---:|---:|---|"]
        for sh in ships:
            before = [d for d in days_all if d[0] < sh["date"]][-10:]
            after = [d for d in days_all if d[0] >= sh["date"]]
            nb = f"{sum(_day_means(before))/len(before):+.1f}" if before else "-"
            na = f"{sum(_day_means(after))/len(after):+.1f}" if after else "-"
            read = "NOT ENOUGH DATA" if len(after) < 10 else ("BETTER" if after and before and sum(_day_means(after))/len(after) > sum(_day_means(before))/len(before) + 2 else "WORSE" if after and before and sum(_day_means(after))/len(after) < sum(_day_means(before))/len(before) - 2 else "NO CHANGE")
            out.append(f"| {sh['date']} | {sh['change'][:70]} | {len(after)} | {nb} | {na} | {read} |")
        out.append("")
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=20)
    n = ap.parse_args().days
    days = []
    for f in sorted(glob.glob(os.path.join(ROOT, "ai_reports/tapes/2026-*/outcomes.jsonl"))):
        d = f.split("/")[-2]
        if len(d) == 10:
            r = load_day(f)
            if r:
                days.append((d, r))
    days_all = list(days)
    days = days[-n:]
    if not days:
        print("no sessions"); return 1
    out = ["# Desk scoreboard", "", f"Rolling {len(days)} sessions ending {days[-1][0]}. Net = exit vs entry less {COST_BP:g} bp. "
           "Never-green = no print at or above entry +5 bp. Dollars are the desk's realized P&L.", "",
           "| day | trades | win % | never-green % | net bp/trade | $ |", "|---|---:|---:|---:|---:|---:|"]
    per_day = []
    for d, r in days:
        nb = sum(x["bp"] for x in r) / len(r)
        per_day.append(nb)
        out.append(f"| {d} | {len(r)} | {100*sum(x['bp']>0 for x in r)/len(r):.0f} | {100*sum(x['ng'] for x in r)/len(r):.0f} | {nb:+.1f} | {sum(x['usd'] for x in r):+.2f} |")
    allr = [x for _, r in days for x in r]
    m = sum(x["bp"] for x in allr) / len(allr)
    se = (st.pstdev(per_day) / math.sqrt(len(per_day))) if len(per_day) > 2 else float("nan")
    last5 = [x for _, r in days[-5:] for x in r]
    line = (f"ROLLING {len(days)}: {len(allr)} trades, net {m:+.1f} bp/trade (day-level SE {se:.1f}), "
            f"win {100*sum(x['bp']>0 for x in allr)/len(allr):.0f}%, never-green {100*sum(x['ng'] for x in allr)/len(allr):.0f}%, "
            f"${sum(x['usd'] for x in allr):+.2f}; last 5 sessions net {sum(x['bp'] for x in last5)/max(1,len(last5)):+.1f} bp/trade")
    out += ["", f"**{line}**", ""]
    out += payoff_block(allr)
    out += edge_block(allr)
    out += trend_and_ships(days_all)
    open(os.path.join(ROOT, "docs/SCOREBOARD.md"), "w").write("\n".join(out))
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
