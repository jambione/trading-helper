#!/usr/bin/env python3
"""SIP day-1 readiness (SIP month plan S1). Read-only: prints which feed and which delay terms the desk uses under
today's config and under the two-knob SIP flip, plus the numbers that were tuned on IEX and must be re-read.

    .venv/bin/python tools/sip_readiness.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alpaca_api as aa  # noqa: E402
from config import load_config_ro  # noqa: E402

FLIP = {"alpaca_bar_feed": "sip", "ai_sip_realtime": True}
RETUNE = [
    ("ai_watch_decision_max_age_sec", "stale-tape ceiling: tuned for IEX gaps; SIP prints every 2-9 s"),
    ("ai_watch_tape_gap_mult", "per-name tape-gap ceiling multiplier"),
    ("ai_watch_tape_gap_max_sec", "per-name tape-gap ceiling cap"),
    ("ai_watch_quote_max_age_sec", "quote freshness for the arm"),
    ("ai_watch_max_sip_spread_pct", "spread cap: IEX spreads read 1.5x-200x too wide; real SIP spreads are ~3 bp"),
    ("ai_watch_min_rvol", "RVOL floor: numerator and denominator must be one feed (rebuild cache/avg_session_volume.json on SIP daily bars)"),
    ("ai_watch_movers_min_rvol", "movers RVOL floor"),
    ("ai_phase_b_enabled", "premarket lane (dry run today)"),
    ("ai_phase_b_dry_run", "premarket lane: dry until the premarket branch is merged and SIP prints flow"),
]


def show(label: str, cfg: dict) -> None:
    feed = aa.live_data_feed(cfg)
    env = (os.getenv("ALPACA_BAR_FEED") or "").strip().lower()
    print(f"== {label}")
    print(f"   live feed (alpaca_api.live_data_feed): {feed.value}   signal_engine env ALPACA_BAR_FEED: {env or '(unset: reads bot_config)'}")
    print(f"   pacing delay ai_movers_sip_delay_min: {aa.sip_delay_min(float(cfg.get('ai_movers_sip_delay_min', 15.0)), cfg):g} min"
          f"   spread/gap read delay: {aa.sip_delay_min(16.0, cfg):g} min")


def main() -> int:
    base = dict(load_config_ro() or {})
    show("today", base)
    show("after the flip (alpaca_bar_feed=sip, ai_sip_realtime=true)", {**base, **FLIP})
    print("== re-read on SIP day (current values)")
    for k, why in RETUNE:
        print(f"   {k} = {base.get(k)!s:8}  {why}")
    print("== flip: set the two knobs in config/bot_config.json AND ALPACA_BAR_FEED=sip in signal_engine.env, restart,")
    print("   then rerun this tool and check each line above reads sip / 0 min. Rollback = the same two knobs back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
