#!/usr/bin/env python3
"""Server-side Claude research desk.

Runs the scheduled Claude research prompt, turns qualifying ideas into real
Alpaca bracket orders, and enforces the stop / scale-out / trailing / time-stop
rules mechanically. Publishes two JSON files that dashboard.py merges into
/api/state:

    claude_suggestions.json      ranked ideas + quotes (the CLAUDE panel)
    claude_positions_state.json  open positions, resting orders, performance

The momentum monitor used to do all of this itself, which meant it only
happened while a desk terminal was open. It is now a renderer: it reads these
through /api/state and originates nothing.

Entry evaluation and the thesis-break review are the only model calls — both
ride inside claude_suggest.call_claude(). Everything after a position opens is
plain broker arithmetic in claude_positions.manage_open_positions().

    python3 claude_trader.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ENV_FILE_KEYS: list = []


def _load_env_file(path: Path) -> None:
    """Parse KEY=VALUE lines into os.environ. Shell environment always wins."""
    if not path.exists():
        return
    loaded = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split(" #", 1)[0].strip()
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    if loaded:
        _ENV_FILE_KEYS.extend(loaded)
        print(f"[ENV] Loaded {len(loaded)} setting(s) from signal_engine.env",
              flush=True)


# Alpaca credentials live only in signal_engine.env; config.load_config() never
# sees them. Must run before anything imports a broker client.
_load_env_file(ROOT / "signal_engine.env")

import claude_positions  # noqa: E402
from claude_suggest import ClaudeSuggestions  # noqa: E402
from config import load_config  # noqa: E402

SUGGESTIONS_FILE = ROOT / "claude_suggestions.json"
POSITIONS_FILE = ROOT / "claude_positions_state.json"

LOOP_SLEEP = 5.0


def _write_json(path: Path, payload: dict) -> None:
    """Atomic replace — dashboard.py may read this file mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception:
            os.close(fd)
            raise
        Path(tmp_path).replace(path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _suggestions_payload(gs: ClaudeSuggestions, now: float) -> dict:
    """Every key here backs something the monitor's CLAUDE panel renders."""
    return {
        "updated": now,
        "last_ok": gs.last_ok,
        "error": gs.error,
        "quotes_error": gs.quotes_error,
        "last_quote_ok": gs.last_quote_ok,
        "model": gs.model,
        "backend": gs.backend,
        "trading": gs.trading,
        "trading_mode": gs.trading_mode,
        "max_price": gs.max_price,
        "next_run_label": gs.next_run_label(now),
        "last_report_path": gs.last_report_path,
        "last_trades": gs.last_trades,
        "rows": list(gs.rows),
    }


def _positions_payload(mode: str, now: float) -> dict:
    import alpaca_trader

    error = ""
    positions: dict = {}
    open_orders: list = []
    if alpaca_trader.is_active():
        detail = alpaca_trader.get_positions_detail()
        if detail is None:
            error = "Alpaca position query failed"
        else:
            positions = detail
        open_orders = alpaca_trader.get_open_orders()
    else:
        error = "trader inactive — no Alpaca session"

    try:
        performance = claude_positions.performance_summary()
    except Exception as e:  # noqa: BLE001
        performance = {"count": 0}
        error = error or f"performance_summary failed: {e}"

    return {
        "updated": now,
        "mode": mode,
        "error": error,
        "positions": positions,
        "open_orders": open_orders,
        "performance": performance,
    }


def _build_suggestions(cfg: dict) -> ClaudeSuggestions:
    trading = bool(cfg.get("claude_trading_enabled", False))
    return ClaudeSuggestions(
        max_price=cfg.get("claude_max_price"),
        quote_interval=float(cfg.get("claude_quote_poll", 15.0)),
        volume_interval=float(cfg.get("claude_volume_poll", 60.0)),
        avg_days=int(cfg.get("claude_avg_days", 10)),
        rvol_time_adjusted=bool(cfg.get("claude_rvol_time_adjusted", True)),
        model=cfg.get("claude_model", "sonnet"),
        prompt_file=cfg.get("claude_prompt_file", "claude_prompt.txt"),
        request_timeout=float(cfg.get("claude_request_timeout", 600.0)),
        live_search=bool(cfg.get("claude_live_search", True)),
        save_reports=bool(cfg.get("claude_save_reports", True)),
        trading=trading,
        trade_amount=float(cfg.get("claude_trade_amount", 1000.0)),
        max_positions=int(cfg.get("claude_max_positions", 5)),
        max_buys_per_poll=int(cfg.get("claude_max_buys_per_poll", 3)),
        max_sells_per_poll=int(cfg.get("claude_max_sells_per_poll", 5)),
        max_turns=int(cfg.get("claude_max_turns", 8)),
        max_output_tokens=int(cfg.get("claude_max_output_tokens", 10000)),
        search_tools=cfg.get("claude_search_tools", "web"),
        use_prior_context=bool(cfg.get("claude_use_prior_context", True)),
        backend=cfg.get("claude_backend", "claude_cli"),
        cli_bin=cfg.get("claude_cli_bin", "claude"),
        effort=cfg.get("claude_effort", "xhigh"),
        research_times=cfg.get("claude_research_times",
                               ["04:00", "11:00", "13:00"]),
        research_weekdays_only=bool(
            cfg.get("claude_research_weekdays_only", True)),
        research_catchup_min=int(cfg.get("claude_research_catchup_min", 120)),
        risk_pct=float(cfg.get("claude_risk_pct", 1.0)),
        trade_style=cfg.get("claude_trade_style", "Moderate position"),
        min_reward_risk=float(cfg.get("claude_min_reward_risk", 3.0)),
    )


def main() -> None:
    cfg = load_config()

    if not cfg.get("claude_research_enabled", False):
        print("[claude] claude_research_enabled is false — nothing to run.",
              flush=True)
        return

    gs = _build_suggestions(cfg)
    positions_poll = float(cfg.get("claude_positions_poll_sec", 5.0))

    print(f"[claude] backend={gs.backend} model={gs.model} "
          f"trading={gs.trading} mode={gs.trading_mode}", flush=True)
    print(f"[claude] research times {gs.research_times or '(interval)'} ET — "
          f"next {gs.next_run_label() or 'n/a'}", flush=True)
    if gs.trading and gs.trading_mode == "off":
        print("[claude] WARNING: trading requested but no Alpaca session — "
              "check signal_engine.env", flush=True)

    last_positions_tick = 0.0
    while True:
        t0 = time.time()

        # refresh() self-throttles to the research schedule and does the slow
        # CLI call on its own thread, so this is nearly free between slots.
        if not gs.refresh(t0):
            gs.refresh_quotes(t0)
            gs.refresh_volume(t0)

        if gs.trading and (t0 - last_positions_tick) >= positions_poll:
            last_positions_tick = t0
            try:
                claude_positions.manage_open_positions(t0)
            except Exception as e:  # noqa: BLE001
                print(f"[claude] manage_open_positions failed: {e}", flush=True)

        _write_json(SUGGESTIONS_FILE, _suggestions_payload(gs, t0))
        _write_json(POSITIONS_FILE, _positions_payload(gs.trading_mode, t0))

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[claude] stopped.", flush=True)
