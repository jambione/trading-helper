#!/usr/bin/env python3
"""Grade the operator's edge: stage-1 names, then stage-2 timing.

Stage 1 (which names) already lives in setup_rules.evaluate / universe
``setup``. Stage 2 (when) was never scored on those names — only on the
unfiltered squeeze list, which is a different question.

This screen does not arm anything. It asks whether, on names that already
clear the five-leg conjunction, the first tick where both %R lines are
rising together AND RSI is at the bottom turning up has playable tape
versus the same names with no timing.

Bar is imported from universe_screen (median MFE >= 2x own round trip,
MFE/MAE >= 1.2, >=70% sessions green). Do not edit those constants here.

Usage (mini, venv — needs Alpaca bars):

    .venv/bin/python tools/setup_screen.py --days 20 --horizons 15,30,60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import drift_screen as DS  # noqa: E402
import setup_rules as SR  # noqa: E402
import universe_screen as US  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"


def _stage2_from_row(r: dict) -> dict:
    return SR.stage2(
        pctr_rising=r.get("pctr_rising"),
        pctr_slow_rising=r.get("pctr_slow_rising"),
        pctr_slow_falling=r.get("pctr_slow_falling"),
        cm_rsi=r.get("cm_rsi"),
        cm_rsi_rising=r.get("cm_rsi_rising"),
    )


def load_setup_entry(days: int, max_shares_m: float) -> dict[str, dict[str, float]]:
    """First shadow tick where stage 1 AND stage 2 entry_ok both hold.

    Prefer the live ``setup_ok`` / ``setup_entry_ok`` stamps. Fall back to
    recomputing from the same row when an older trader had not logged them.
    Unknown timing never qualifies.
    """
    try:
        import float_feed
    except ImportError:
        float_feed = None
    news = {}
    try:
        news = json.loads(
            (Path(ROOT) / "ai_reports" / "news_cache.json").read_text(
                encoding="utf-8"))
    except (OSError, ValueError):
        news = {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for ts, r in US._iter_log("shadow.jsonl", days):
        sym = r.get("symbol")
        if not sym:
            continue
        sym = str(sym).upper()
        day = DS._day_of(ts)
        if sym in out.get(day, {}):
            continue
        s1 = r.get("setup_ok")
        if s1 is None and float_feed is not None:
            items = news.get(sym) or []
            n24 = sum(1 for n in items
                      if n.get("ts") is not None
                      and ts - 24 * 3600 <= n["ts"] < ts)
            s1 = SR.evaluate(
                pct_change=r.get("pct_change") or r.get("admit_pct_change"),
                rvol=r.get("rvol") or r.get("admit_rvol"),
                price=r.get("price"),
                shares_out_m=float_feed.shares_out(sym),
                news_n_24h=n24 if n24 else r.get("news_n_24h"),
                news_mins_since=r.get("news_mins_since"),
                max_shares_out_m=max_shares_m,
            )["ok"]
        if s1 is not True:
            continue
        s2_ok = r.get("setup_entry_ok")
        if s2_ok is None:
            s2_ok = _stage2_from_row(r).get("entry_ok")
        if s2_ok is not True:
            continue
        out[day][sym] = US._clamp_to_rth(ts, day)
    return dict(out)


def _print_block(name: str, plan: dict, bars: dict, horizons: list[int],
                 stride_arg: int, cost_model: str, payload: dict) -> None:
    n_names = sum(len(v) for v in plan.values())
    hdr = (f"{'cell':<18}{'horiz':>6}{'names':>7}{'n':>7}{'sess':>5}"
           f"{'medMFE':>8}{'M/A':>6}{'payX':>6}{'clear':>7}"
           f"{'green':>7}{'drift':>11}{'play':>12}")
    print(hdr)
    print("-" * len(hdr))
    if not plan:
        print(f"{name:<18}   (empty — no stage-2 fires in the window)")
        print()
        return
    for hz in horizons:
        stride = stride_arg or hz
        rows = []
        for day, members in plan.items():
            for sym, elig in members.items():
                b = bars.get(sym)
                if not b:
                    continue
                cost, _src = US.name_cost_pct(b, day, cost_model)
                got = DS.sample_excursions(
                    b, day, hz, stride, float(elig or 0.0), True)
                for r in got:
                    r["cost"] = cost
                rows.extend(got)
        s = DS.score(rows)
        p = (US.playability(rows, s) if s["verdict"] != "EMPTY"
             else {"verdict": "EMPTY"})
        payload[f"{name}@{hz}m"] = {"drift": s, "playable": p,
                                    "name_days": n_names}
        if s["verdict"] == "EMPTY":
            print(f"{name:<18}{hz:>6}{n_names:>7}{0:>7}   (no samples)")
            continue
        print(f"{name:<18}{hz:>6}{n_names:>7}{s['n']:>7}{s['sessions']:>5}"
              f"{s['median_mfe']:>8.3f}"
              f"{(s['mfe_over_mae'] or 0):>6.2f}"
              f"{(p.get('pay_x') or 0):>6.2f}"
              f"{(p.get('pct_clearing_cost') or 0):>7.0%}"
              f"{s['sessions_green']}/{s['sessions']:<4}"
              f"{s['verdict']:>11}{p['verdict']:>12}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--horizons", default="15,30,60")
    ap.add_argument("--stride", type=int, default=0)
    ap.add_argument("--max-shares-m", type=float, default=10.0)
    ap.add_argument("--cost-model", default="measured",
                    choices=("measured", "fixed"))
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    print("setup screen  stage1 vs stage1+stage2 entry")
    print(f"  days={args.days}  horizons={horizons}min  "
          f"shares<{args.max_shares_m}M")
    print("  DOES NOT ARM. Gate 1 (min-hold) still owns the live book.\n")

    s1 = US.load_setup(args.days, args.max_shares_m)
    s2 = load_setup_entry(args.days, args.max_shares_m)
    pool: set[str] = set()
    for plan in (s1, s2):
        for d in plan.values():
            pool.update(d)
    if not pool:
        print("no setup names in the window — is shadow.jsonl on this machine?")
        return 0

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 5)
    bars = DS.fetch_minutes(sorted(pool), start, end)
    if not bars:
        print("  no Alpaca data client — run on the mini with .venv.")
        return 0

    payload: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "max_shares_m": args.max_shares_m,
        "note": "lab only — not an arm",
    }
    _print_block("setup", s1, bars, horizons, args.stride,
                 args.cost_model, payload)
    _print_block("setup_s2", s2, bars, horizons, args.stride,
                 args.cost_model, payload)
    print("setup = stage 1 at first instant all five legs held.")
    print("setup_s2 = first tick stage 1 AND both %R rising AND RSI 0-50 rising.")
    print("Both DRIFT and PLAYABLE must pass. Thin n is not a pass.")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREEN_DIR / "setup_screen.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
