#!/usr/bin/env python3
"""scorecard.py — the four numbers that say whether the desk is making headway.

    1. OPPORTUNITIES   names on the book through the day, and fills per hour
    2. ENTRY QUALITY   does a fill go +1% before -1%, against a random later
                       minute in the same name (the control)
    3. RUNWAY          how far price ran after the fill (15/30 min, from bars)
    4. CAPTURE         how much of the run the exit kept, and the net result
                       per trade after costs

Every number is measured the same way on every run so days and configs are
comparable. Group by day (default), config_fp or git_version.

SOURCES
    ai_reports/outcomes.jsonl   fills: price, exit, realized P/L, config_fp
    ai_reports/events.jsonl     admit_funnel rows: book size through the day
    SIP 1m bars                 runway and control, via tools/runway_study.py
                                (same cache, same score_path, same control:
                                the symbol-day's minutes after its first fill)

HONESTY
    • Fills with no bars are counted in the P/L columns and left out of the
      bar columns, never zero-filled. Today's bars stop 16 minutes ago (SIP
      delay), so the newest fills score late.
    • Book size (pillar 1) is recorded per day and half hour, not per config:
      funnel rows carry no config_fp.
    • Groups under 30 fills are flagged as anecdotes. The t on entry quality
      is the fill-minus-control difference; |t| < 2 means "no evidence".
    • 1-minute bars, no spread: runway and control are the tape's, not the
      fill's execution.

USAGE (on the mini)
    .venv/bin/python tools/scorecard.py                   # last 7 sessions by day
    .venv/bin/python tools/scorecard.py --by config_fp --since 2026-09-23
    .venv/bin/python tools/scorecard.py --md ai_reports/scorecard.md
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
EVENTS = os.path.join(ROOT, "ai_reports", "events.jsonl")
BOOK_TARGET = 10          # the user's floor for names on the book
ARM_PCT = 0.3             # trail arms at +0.3% (arm_r 0.06, 1R = 5%)


# ── inputs ──────────────────────────────────────────────────────────────
def load_fills(since: str) -> list[dict]:
    out = []
    for line in open(OUTCOMES):
        try:
            r = json.loads(line)
        except Exception:
            continue
        sym, et, px = str(r.get("symbol") or ""), r.get("entry_time"), r.get("entry_price")
        q, pl = r.get("total_qty"), r.get("realized_pl_usd")
        if (not rs.SYM_RE.match(sym) or not isinstance(et, (int, float)) or not px
                or not isinstance(q, (int, float)) or q <= 0
                or not isinstance(pl, (int, float))):
            continue
        if bars.day_of(et) < since:
            continue
        r["_day"] = bars.day_of(et)
        r["_ret"] = pl / (float(px) * float(q)) * 100.0
        e, s = float(px), r.get("stop_price")
        xs = r.get("exit_slippage_r")
        r["_exit_slip_pct"] = (xs * (e - s) / e * 100.0
                               if isinstance(xs, (int, float)) and s and e > s else None)
        pk = r.get("peak_price")
        r["_peak_pct"] = ((float(pk) / e - 1) * 100.0) if pk else None
        out.append(r)
    out.sort(key=lambda r: r["entry_time"])
    return out


def book_sizes(since: str) -> dict:
    """{day: {half_hour 'HH:MM': [kept_n, ...]}} from admit_funnel rows."""
    t0 = time.mktime(time.strptime(since, "%Y-%m-%d"))
    out: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    with open(EVENTS) as f:
        for line in f:
            if '"admit_funnel"' not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get("ts") or 0
            if ts < t0:
                continue
            m = bars.et_minutes(ts)
            if m < 9 * 60 + 30 or m >= 16 * 60:
                continue
            k = e.get("kept_n")
            if not isinstance(k, int):
                continue
            hh = f"{m // 60:02d}:{(m % 60) // 30 * 30:02d}"
            out[bars.day_of(ts)][hh].append(k)
    return out


def score_bars(fills: list[dict]) -> None:
    """Attach runway/control fields to each fill in place (from SIP 1m bars)."""
    cache = rs._load_cache()
    want: dict = {}
    for r in fills:
        m = bars.et_minutes(r["entry_time"])
        if 9 * 60 + 31 <= m <= 15 * 60 + 30:
            want.setdefault(r["_day"], set()).add(r["symbol"])
    rs.fetch_days(want, cache)
    first: dict = {}
    for r in fills:
        k = (r["symbol"], r["_day"])
        first[k] = min(first.get(k, r["entry_time"]), r["entry_time"])
    ctrl: dict = {}
    for k, t0 in first.items():
        B = cache.get(k)
        if not B:
            continue
        tp, r30 = [], []
        for i in range(0, len(B[0]), 5):
            if B[0][i] >= t0 and bars.et_minutes(B[0][i]) <= 15 * 60 + 30:
                s = rs.score_path(B, i, B[4][i])
                if s:
                    if s.get("tp_1.0") is not None:
                        tp.append(s["tp_1.0"])
                    if s.get("ret_30") is not None:
                        r30.append(s["ret_30"])
        ctrl[k] = (rs.mean(tp) if tp else None, statistics.median(r30) if r30 else None)
    for r in fills:
        k = (r["symbol"], r["_day"])
        B = cache.get(k)
        if not B:
            continue
        i = bars.index_at(B[0], r["entry_time"])
        if i < 0:
            continue
        s = rs.score_path(B, i, float(r["entry_price"]))
        if not s:
            continue
        r["_bars"] = s
        r["_ctrl"] = ctrl.get(k)


# ── pillars ─────────────────────────────────────────────────────────────
def _pct(xs, p):
    return rs.q(xs, p) if xs else None


def _t(xs):
    if len(xs) < 5:
        return None
    sd = statistics.stdev(xs)
    return (rs.mean(xs) / (sd / math.sqrt(len(xs)))) if sd > 0 else None


def pillars(fs: list[dict]) -> dict:
    n = len(fs)
    rets = [r["_ret"] for r in fs]
    pl = sum(r["realized_pl_usd"] for r in fs)
    hours = collections.Counter(bars.et_minutes(r["entry_time"]) // 60 for r in fs)
    sb = [r for r in fs if r.get("_bars")]
    tp = [r["_bars"]["tp_1.0"] for r in sb if r["_bars"].get("tp_1.0") is not None]
    tp_ctrl = [r["_ctrl"][0] for r in sb
               if r["_bars"].get("tp_1.0") is not None and r.get("_ctrl") and r["_ctrl"][0] is not None]
    d30 = [r["_bars"]["ret_30"] - r["_ctrl"][1] for r in sb
           if r["_bars"].get("ret_30") is not None and r.get("_ctrl") and r["_ctrl"][1] is not None]
    up15 = [r["_bars"]["up_15"] for r in sb if r["_bars"].get("up_15") is not None]
    up30 = [r["_bars"]["up_30"] for r in sb if r["_bars"].get("up_30") is not None]
    never = [r for r in fs if r["_peak_pct"] is not None and r["_peak_pct"] <= 1e-9]
    # Capture: of the run available in the 30 minutes after the fill, how much
    # did the exit keep. Summed, not averaged per trade, so one tiny runway
    # cannot produce a 900% ratio.
    cap_pairs = [(r["_ret"], r["_bars"]["up_30"]) for r in sb
                 if r["_bars"].get("up_30") is not None and r["_bars"]["up_30"] > 0]
    capture = (sum(a for a, _ in cap_pairs) / sum(b for _, b in cap_pairs)
               if cap_pairs and sum(b for _, b in cap_pairs) > 0 else None)
    beat_hold = [r["_ret"] - r["_bars"]["ret_30"] for r in sb if r["_bars"].get("ret_30") is not None]
    slip = [r["_exit_slip_pct"] for r in fs if r["_exit_slip_pct"] is not None]
    return {
        "n": n, "n_bars": len(sb), "pl": pl,
        "net_pct": rs.mean(rets), "win": (sum(x > 0 for x in rets) / n) if n else None,
        "hours_active": len(hours), "fills_by_hour": dict(sorted(hours.items())),
        "tp1": rs.mean(tp) if tp else None, "tp1_ctrl": rs.mean(tp_ctrl) if tp_ctrl else None,
        "tp1_n": len(tp), "d30": rs.mean(d30) if d30 else None, "d30_t": _t(d30),
        "never_green": (len(never) / n) if n else None,
        "up15_med": _pct(up15, .5), "up30_med": _pct(up30, .5),
        "reach_arm": (sum(x >= ARM_PCT for x in up15) / len(up15)) if up15 else None,
        "reach_1": (sum(x >= 1.0 for x in up30) / len(up30)) if up30 else None,
        "capture": capture, "beat_hold": rs.mean(beat_hold) if beat_hold else None,
        "exit_slip": rs.mean(slip) if slip else None,
    }


def fmt(v, kind):
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:+.2f}%"
    if kind == "share":
        return f"{v:.0%}"
    if kind == "usd":
        return f"${v:+.2f}"
    if kind == "int":
        return str(int(v))
    if kind == "t":
        return f"{v:+.1f}"
    return f"{v:.2f}"


ROWS = [
    ("OPPORTUNITIES", None, None),
    ("fills", "n", "int"),
    ("hours with a fill", "hours_active", "int"),
    ("ENTRY QUALITY", None, None),
    ("+1% before -1% (fills)", "tp1", "share"),
    ("+1% before -1% (control)", "tp1_ctrl", "share"),
    ("30m vs control, pp", "d30", "pct"),
    ("  t", "d30_t", "t"),
    ("never above the fill", "never_green", "share"),
    ("RUNWAY", None, None),
    ("median best move, 15m", "up15_med", "pct"),
    ("median best move, 30m", "up30_med", "pct"),
    (f"reach +{ARM_PCT}% (arm) in 15m", "reach_arm", "share"),
    ("reach +1% in 30m", "reach_1", "share"),
    ("CAPTURE", None, None),
    ("kept of 30m runway", "capture", "share"),
    ("exit vs holding 30m, pp", "beat_hold", "pct"),
    ("exit slippage under stop", "exit_slip", "pct"),
    ("BOTTOM LINE", None, None),
    ("net per trade", "net_pct", "pct"),
    ("win rate", "win", "share"),
    ("P/L", "pl", "usd"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=(datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"))
    ap.add_argument("--by", choices=("day", "config_fp", "git_version"), default="day")
    ap.add_argument("--md", help="also write a markdown copy here")
    ap.add_argument("--json", help="also write raw numbers here")
    args = ap.parse_args()

    fills = load_fills(args.since)
    score_bars(fills)
    key = {"day": lambda r: r["_day"],
           "config_fp": lambda r: str(r.get("config_fp") or "unknown"),
           "git_version": lambda r: str(r.get("git_version") or "unknown")}[args.by]
    groups: dict = collections.OrderedDict()
    for r in fills:
        groups.setdefault(key(r), []).append(r)
    res = {g: pillars(fs) for g, fs in groups.items()}
    firsts = {g: time.strftime("%m-%d %H:%M", time.localtime(fs[0]["entry_time"]))
              for g, fs in groups.items()}

    cols = list(res)[-8:]
    w = 14
    lines = [f"SCORECARD by {args.by} since {args.since}"
             f"  (fills {len(fills)}, with bars {sum(1 for r in fills if r.get('_bars'))})", ""]
    lines.append(f"{'':<30}" + "".join(f"{c[:w - 1]:>{w}}" for c in cols))
    if args.by != "day":
        lines.append(f"{'first fill':<30}" + "".join(f"{firsts[c]:>{w}}" for c in cols))
    for label, k, kind in ROWS:
        if k is None:
            lines.append(f"── {label}")
            continue
        lines.append(f"{label:<30}" + "".join(f"{fmt(res[c][k], kind):>{w}}" for c in cols))
    flags = [c for c in cols if res[c]["n"] < 30]
    if flags:
        lines.append(f"\n[n<30: anecdote] {', '.join(c[:12] for c in flags)}")

    # Book size is per day and half hour (funnel rows carry no config_fp).
    bs = book_sizes(args.since)
    lines.append(f"\n── BOOK SIZE (names kept by the funnel; target >= {BOOK_TARGET})")
    for day in sorted(bs)[-5:]:
        cells = []
        at_target = 0
        halves = sorted(bs[day])
        for hh in halves:
            v = bs[day][hh]
            med = statistics.median(v)
            at_target += med >= BOOK_TARGET
            cells.append(f"{hh} {med:>4.1f}")
        lines.append(f"  {day}: half-hours at target {at_target}/{len(halves)}")
        for i in range(0, len(cells), 7):
            lines.append("    " + "   ".join(cells[i:i + 7]))

    text = "\n".join(lines)
    print(text)
    if args.md:
        with open(args.md, "w") as f:
            f.write("```\n" + text + "\n```\n")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"by": args.by, "since": args.since, "groups": res}, f, default=str)


if __name__ == "__main__":
    main()
