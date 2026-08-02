"""One-off manual trigger for Anthropic AI research, bypassing the weekday/
time-slot schedule. Builds the same AiSuggestions ai_trader.py runs,
then forces research_times empty so refresh() fires immediately instead of
waiting for the next 04:00/11:00/13:00 weekday slot.

Publishes results to claude_suggestions.json (same payload as ai_trader.py)
so the dashboard / momentum monitor AI table can show them.

Trading follows config/bot_config.json's ai_trading_enabled — pass
--no-trade to research without placing any orders.

Run: .venv/bin/python _manual_research_run.py [--no-trade]
"""
from __future__ import annotations

import sys
import time

import ai_trader
from config import load_config


def main() -> int:
    cfg = load_config()
    if "--no-trade" in sys.argv:
        cfg = {**cfg, "ai_trading_enabled": False, "claude_trading_enabled": False}

    gs = ai_trader._build_suggestions(cfg)
    gs.research_times = []  # forced immediate run — bypasses the weekday/slot gate

    print(f"backend={gs.backend} model={gs.model} effort={gs.effort} "
          f"trading={gs.trading} trading_mode={gs.trading_mode} "
          f"live_search={gs.live_search}", flush=True)

    gs.refresh()
    if not gs._fetching:
        print(f"refresh() did not start a worker — error={gs.error!r}", flush=True)
        return 1

    print("research started, waiting...", flush=True)
    started = time.time()
    while gs._fetching:
        time.sleep(5)
        print(f"...still running ({time.time() - started:.0f}s)", flush=True)

    # Best-effort quote enrich before publish (matches grok manual path)
    try:
        now_enrich = time.time()
        gs.refresh_quotes(now_enrich)
        gs.refresh_volume(now_enrich)
    except Exception as e:  # noqa: BLE001
        print(f"quote enrich skipped: {e}", flush=True)

    now = time.time()
    path = ai_trader.CLAUDE_SUGGESTIONS_FILE
    payload = ai_trader._suggestions_payload(
        gs, now, path=path, source=ai_trader.SOURCE_ANTHROPIC)
    ai_trader._write_json(path, payload)

    print(f"\ndone in {time.time() - started:.0f}s", flush=True)
    if gs.error:
        print(f"error: {gs.error}", flush=True)
    print(f"published: {path}", flush=True)
    print(f"report: {gs.last_report_path or '(none)'}", flush=True)
    print(f"suggestions: {len(payload['rows'])}", flush=True)
    for r in payload["rows"]:
        score = r.get("trending_score", r.get("score"))
        print(f"  A  {r.get('symbol'):<6} score={score} "
              f"{(r.get('reason') or '')[:100]}", flush=True)
    if gs.last_trades:
        print(f"\npaper trades ({len(gs.last_trades)}):", flush=True)
        for t in gs.last_trades:
            print(f"  {t}")
    if gs.last_usage:
        print(f"\nusage: {gs.last_usage}", flush=True)

    return 0 if payload["rows"] or not gs.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
