#!/usr/bin/env python3
"""One-off Grok (xAI subscription) research run → grok_suggestions.json.

Uses the same AiSuggestions engine with backend=cli / grok login.
Does **not** trade. Bypasses the weekday/time schedule for an immediate run.

    .venv/bin/python _manual_grok_research_run.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import desk_core  # noqa: E402

_load_env = desk_core.load_env_file


_load_env()

from ai_suggest import (  # noqa: E402
    SOURCE_XAI,
    AiSuggestions,
    source_from_backend,
)
from config import load_config  # noqa: E402

GROK_FILE = ROOT / "grok_suggestions.json"


_write_json = desk_core.write_json_atomic


def _build_grok(cfg: dict) -> AiSuggestions:
    return AiSuggestions(
        max_price=cfg.get("grok_max_price", cfg.get("claude_max_price", 100.0)),
        quote_interval=float(cfg.get("claude_quote_poll", 15.0)),
        volume_interval=float(cfg.get("claude_volume_poll", 60.0)),
        avg_days=int(cfg.get("claude_avg_days", 10)),
        rvol_time_adjusted=bool(cfg.get("claude_rvol_time_adjusted", True)),
        model=cfg.get("grok_model", "grok-4.5"),
        prompt_file=cfg.get("grok_prompt_file",
                            cfg.get("claude_prompt_file", "ai_prompt.txt")),
        request_timeout=float(cfg.get("grok_request_timeout",
                                      cfg.get("claude_request_timeout", 600.0))),
        live_search=bool(cfg.get("grok_live_search", True)),
        save_reports=bool(cfg.get("grok_save_reports", True)),
        trading=False,  # research-only — never place orders from this path
        max_turns=int(cfg.get("grok_max_turns", 4)),
        search_tools=cfg.get("grok_search_tools",
                             cfg.get("claude_search_tools", "web_x")),
        use_prior_context=bool(cfg.get("grok_use_prior_context", False)),
        use_desk_snapshot=bool(cfg.get(
            "grok_use_desk_snapshot",
            cfg.get("ai_use_desk_snapshot", True))),
        backend=cfg.get("grok_backend", "cli"),
        cli_bin=cfg.get("grok_cli_bin", "grok"),
        research_times=[],  # force immediate (manual)
        research_weekdays_only=False,
    )


def _payload(gs: AiSuggestions, now: float) -> dict:
    # Ensure every row is tagged xAI even if parse path missed it.
    src = source_from_backend(gs.backend)
    rows = []
    for r in gs.rows:
        row = dict(r)
        row["source"] = src if src != "unknown" else SOURCE_XAI
        row["source_mark"] = "X"
        rows.append(row)
    return {
        "updated": now,
        "last_ok": gs.last_ok,
        "error": gs.error,
        "quotes_error": gs.quotes_error,
        "last_quote_ok": gs.last_quote_ok,
        "model": gs.model,
        "backend": gs.backend,
        "source": SOURCE_XAI,
        "trading": False,
        "trading_mode": "off",
        "max_price": gs.max_price,
        "next_run_label": "",
        "last_report_path": gs.last_report_path,
        "last_trades": [],
        "rows": rows,
    }


def main() -> int:
    cfg = load_config()
    gs = _build_grok(cfg)
    print(f"backend={gs.backend} model={gs.model} max_turns={gs.max_turns} "
          f"trading={gs.trading} live_search={gs.live_search}", flush=True)

    gs.refresh()
    if not gs._fetching:
        print(f"refresh() did not start — error={gs.error!r}", flush=True)
        return 1

    print("Grok research started (subscription CLI)…", flush=True)
    started = time.time()
    while gs._fetching:
        time.sleep(5)
        print(f"  …still running ({time.time() - started:.0f}s)", flush=True)

    # Best-effort quote enrich before publish
    try:
        gs.refresh_quotes(time.time())
        gs.refresh_volume(time.time())
    except Exception as e:  # noqa: BLE001
        print(f"quote enrich skipped: {e}", flush=True)

    now = time.time()
    payload = _payload(gs, now)
    _write_json(GROK_FILE, payload)

    print(f"\ndone in {time.time() - started:.0f}s", flush=True)
    if gs.error:
        print(f"error/status: {gs.error}", flush=True)
    print(f"published: {GROK_FILE}", flush=True)
    print(f"report: {gs.last_report_path or '(none)'}", flush=True)
    print(f"suggestions: {len(payload['rows'])}", flush=True)
    for r in payload["rows"]:
        print(f"  X  {r.get('symbol'):<6} score={r.get('trending_score')} "
              f"{(r.get('reason') or '')[:70]}", flush=True)
    if gs.last_usage:
        u = gs.last_usage
        print(f"\nusage: cost={u.get('total_cost_usd')} "
              f"in={u.get('input_tokens')} out={u.get('output_tokens')} "
              f"turns={u.get('num_turns')}", flush=True)
    return 0 if payload["rows"] or not gs.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
