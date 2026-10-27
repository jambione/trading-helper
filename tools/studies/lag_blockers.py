#!/usr/bin/env python3
"""lag_blockers.py (DIAGNOSTIC, no pass/fail): which board blocker held each bought name, minute by minute, between the
%R turn and the fill. Input: ~/replay_runs/lag_breakdown.json (tools/studies/lag_breakdown.py) and the recorded
ai_reports/entry_watch_state.json snapshots in ~/session_snapshots/<day>/state_snapshots.jsonl.gz.

Each minute from the turn's close to the fill gets the block_code seen most often in that minute's snapshots for the
symbol (by last_poll_ts). No snapshot row = 'off_board'; a row with no block_code = 'ready' (arm-ready or armed).
Codes are grouped as below; unlisted codes go to 'other'.
USAGE (mini): .venv/bin/python tools/studies/lag_blockers.py
"""
from __future__ import annotations

import gzip
import json
import os
import statistics
from collections import Counter, defaultdict

SNAP = os.path.expanduser("~/session_snapshots")
IN = os.path.expanduser("~/replay_runs/lag_breakdown.json")

GROUPS = {
    "plumbing": {"stale_quote", "pctr_not_live_alpaca", "pctr_not_live_missing", "vol_now_unknown", "spread_unknown",
                 "no_quote", "tape_only", "gap_unknown", "no_tape", "stale_tape", "macd_src_unknown"},
    "cooldown": {"reentry_cooldown"},
    "strategy": {"exh_not_tight", "exh_falling", "wait_exh", "presquare_confirm", "square_confirm", "wait_mid_rise",
                 "mid_rise_stale", "mid_rise_lost", "stale_square", "not_presquare", "in_square", "exh_not_rising",
                 "exh_rising_required", "wr_not_trending", "wr_no_history", "wr_src_mismatch", "wr_slow_5m_falling",
                 "last_macd_gap", "macd_rsi_hot", "rsi_not_rising", "rsi_extended", "presquare_after_square"},
    "price/zone": {"above_zone", "below_zone", "in_zone", "no_structure", "spread_wide", "extended_cheap",
                   "cheap_ob_band", "below_min_price"},
    "off_board": {"off_board"},
    "ready": {"ready"},
}


def group_of(code: str) -> str:
    for g, s in GROUPS.items():
        if code in s:
            return g
    return "other"


def board_index(day: str, syms: set) -> dict:
    idx = defaultdict(list)
    path = os.path.join(SNAP, day, "state_snapshots.jsonl.gz")
    if not os.path.exists(path):
        return idx
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if "entry_watch_state" not in line[:300]:
                continue
            d = json.loads(line)
            if d.get("file") != "ai_reports/entry_watch_state.json":
                continue
            for s, rec in (d.get("data") or {}).items():
                if s in syms and isinstance(rec, dict):
                    t = rec.get("last_poll_ts") or d.get("ts")
                    idx[s].append((float(t), str(rec.get("block_code") or "ready")))
    for v in idx.values():
        v.sort()
    return idx


def main() -> None:
    rows = [r for r in json.load(open(IN)) if not r.get("skip") and r.get("fill_after_turn_s", -1) > 0]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    mins_code, mins_group, per_buy = Counter(), Counter(), []
    for day, rs in sorted(by_day.items()):
        idx = board_index(day, {r["sym"] for r in rs})
        for r in rs:
            snaps = idx.get(r["sym"], [])
            t, end = r["turn_ts"], r["fill_ts"]
            g_this = Counter()
            while t < end:
                t1 = min(t + 60.0, end)
                w = (t1 - t) / 60.0
                codes = Counter(c for ts, c in snaps if t <= ts < t1)
                code = codes.most_common(1)[0][0] if codes else "off_board"
                mins_code[code] += w
                mins_group[group_of(code)] += w
                g_this[group_of(code)] += w
                t = t1
            per_buy.append(g_this)
    total = sum(mins_group.values())
    print(f"buys {len(per_buy)}, total minutes turn->fill {total:.0f}, mean {total / len(per_buy):.1f} min per buy")
    print("share of minutes by group:")
    for g, m in mins_group.most_common():
        print(f"  {g:11s} {m / total:6.1%}   mean {m / len(per_buy):4.1f} min/buy   "
              f"buys with any {sum(1 for b in per_buy if b.get(g, 0) > 0) / len(per_buy):4.0%}")
    print("top codes by minutes:")
    for c, m in mins_code.most_common(18):
        print(f"  {c:24s} {group_of(c):11s} {m / total:6.1%}")
    pl = [b.get("plumbing", 0) + b.get("cooldown", 0) for b in per_buy]
    print(f"plumbing+cooldown minutes per buy: median {statistics.median(pl):.1f}, mean {statistics.fmean(pl):.1f}; "
          f"buys with >= 3 such minutes {sum(1 for x in pl if x >= 3) / len(pl):.0%}")


if __name__ == "__main__":
    main()
