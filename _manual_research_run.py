"""One-off manual trigger for Claude stock research, bypassing the weekday/
time-slot schedule. Builds the same ClaudeSuggestions claude_trader.py runs,
then forces research_times empty so refresh() fires immediately instead of
waiting for the next 04:00/11:00/13:00 weekday slot.

Trading follows config/bot_config.json's claude_trading_enabled — pass
--no-trade to research without placing any orders.

Run: venv/bin/python _manual_research_run.py [--no-trade]
"""
from __future__ import annotations

import sys
import time

import claude_trader
from config import load_config

cfg = load_config()
if "--no-trade" in sys.argv:
    cfg = {**cfg, "claude_trading_enabled": False}

gs = claude_trader._build_suggestions(cfg)
gs.research_times = []  # forced immediate run — bypasses the weekday/slot gate

print(f"backend={gs.backend} model={gs.model} effort={gs.effort} "
      f"trading={gs.trading} trading_mode={gs.trading_mode} "
      f"live_search={gs.live_search}", flush=True)

gs.refresh()
if not gs._fetching:
    print(f"refresh() did not start a worker — error={gs.error!r}", flush=True)
    sys.exit(1)

print("research started, waiting...", flush=True)
started = time.time()
while gs._fetching:
    time.sleep(5)
    print(f"...still running ({time.time() - started:.0f}s)", flush=True)

print(f"\ndone in {time.time() - started:.0f}s", flush=True)
if gs.error:
    print(f"error: {gs.error}", flush=True)
print(f"report: {gs.last_report_path or '(none)'}", flush=True)
print(f"suggestions: {len(gs.rows)}", flush=True)
for r in gs.rows:
    print(f"  {r.get('symbol'):<6} score={r.get('score')} "
          f"{r.get('reason', '')[:100]}")
if gs.last_trades:
    print(f"\npaper trades ({len(gs.last_trades)}):", flush=True)
    for t in gs.last_trades:
        print(f"  {t}")
print(f"\nusage: {gs.last_usage}", flush=True)
