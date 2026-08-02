#!/usr/bin/env python3
"""Server-side AI research desk (Anthropic Claude by default).

Runs the scheduled research prompt, turns qualifying ideas into real Alpaca
bracket orders, and enforces stop / scale-out / trailing / time-stop rules
mechanically. Publishes JSON that dashboard.py merges into /api/state:

    claude_suggestions.json   Anthropic ranked ideas (source A)
    ai_positions_state.json   open positions, orders, performance
                              (also writes legacy claude_positions_state.json)

Grok / xAI ideas are published separately to grok_suggestions.json (source X);
the dashboard merges both into ai_suggestions (A / X / AX).

The momentum monitor is a renderer only — it reads /api/state and originates
nothing. Only entry evaluation and thesis-break review call a model; open
positions are managed in ai_positions.manage_open_positions().

    python3 ai_trader.py
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

import ai_positions  # noqa: E402
from ai_suggest import AiSuggestions  # noqa: E402
from config import load_config  # noqa: E402

# Per-source idea file (Anthropic). Grok uses grok_suggestions.json.
SUGGESTIONS_FILE = ROOT / "claude_suggestions.json"
# Shared trading book — prefer new name; keep legacy path in sync one release.
POSITIONS_FILE = ROOT / "ai_positions_state.json"
POSITIONS_FILE_LEGACY = ROOT / "claude_positions_state.json"

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


def _read_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _hydrate_suggestions(gs: AiSuggestions) -> int:
    """Load last published ideas so a restart / off-slot loop does not blank the UI."""
    data = _read_json(SUGGESTIONS_FILE)
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not rows:
        return 0
    gs.rows = list(rows)
    gs.by_symbol = {
        str(r.get("symbol") or "").upper(): r
        for r in gs.rows
        if r.get("symbol")
    }
    try:
        gs.last_ok = float(data.get("last_ok") or 0) or 0.0
    except (TypeError, ValueError):
        gs.last_ok = 0.0
    path = data.get("last_report_path") or ""
    if path:
        gs.last_report_path = str(path)
    # Schedule-only notices are not failures once we have displayable rows.
    err = str(gs.error or data.get("error") or "")
    if err.lower().startswith("next research") or err == "no research times configured":
        gs.error = ""
    elif not gs.error and data.get("error"):
        soft = str(data.get("error") or "")
        if not soft.lower().startswith("next research"):
            gs.error = soft
    return len(gs.rows)


def _suggestions_payload(gs: AiSuggestions, now: float) -> dict:
    """Keys the desk AI panel / merge path consume.

    While waiting for the next research slot the in-memory list is often empty
    after a process restart. Prefer last published rows so dashboard/monitor
    keep showing the prior research until a new run replaces them.
    """
    rows = list(gs.rows)
    last_ok = gs.last_ok
    report = gs.last_report_path
    error = gs.error or ""

    if not rows and not last_ok:
        prev = _read_json(SUGGESTIONS_FILE)
        prev_rows = prev.get("rows") if isinstance(prev.get("rows"), list) else []
        try:
            prev_ok = float(prev.get("last_ok") or 0) or 0.0
        except (TypeError, ValueError):
            prev_ok = 0.0
        if prev_rows and prev_ok:
            rows = list(prev_rows)
            last_ok = prev_ok
            report = prev.get("last_report_path") or report

    # "next research run Mon 04:00 ET" is status, not a hard failure when we
    # still have ideas to show.
    if rows and (
        error.lower().startswith("next research")
        or error == "no research times configured"
    ):
        error = ""

    return {
        "updated": now,
        "last_ok": last_ok,
        "error": error,
        "quotes_error": gs.quotes_error,
        "last_quote_ok": gs.last_quote_ok,
        "model": gs.model,
        "backend": gs.backend,
        "source": "anthropic",
        "trading": gs.trading,
        "trading_mode": gs.trading_mode,
        "max_price": gs.max_price,
        "next_run_label": gs.next_run_label(now),
        "last_report_path": report,
        "last_trades": gs.last_trades,
        "rows": rows,
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
        performance = ai_positions.performance_summary()
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


def _cfg(cfg: dict, new_key: str, old_key: str, default=None):
    """Prefer ai_* shared keys; fall back to legacy claude_* names."""
    if new_key in cfg and cfg.get(new_key) is not None:
        return cfg.get(new_key)
    if old_key in cfg and cfg.get(old_key) is not None:
        return cfg.get(old_key)
    return default


def _build_suggestions(cfg: dict) -> AiSuggestions:
    trading = bool(_cfg(cfg, "ai_trading_enabled", "claude_trading_enabled", False))
    return AiSuggestions(
        max_price=_cfg(cfg, "ai_max_price", "claude_max_price"),
        quote_interval=float(_cfg(cfg, "ai_quote_poll", "claude_quote_poll", 15.0)),
        volume_interval=float(_cfg(cfg, "ai_volume_poll", "claude_volume_poll", 60.0)),
        avg_days=int(_cfg(cfg, "ai_avg_days", "claude_avg_days", 10)),
        rvol_time_adjusted=bool(
            _cfg(cfg, "ai_rvol_time_adjusted", "claude_rvol_time_adjusted", True)),
        model=cfg.get("claude_model", "sonnet"),
        prompt_file=cfg.get("claude_prompt_file")
        or cfg.get("ai_prompt_file")
        or "ai_prompt.txt",
        request_timeout=float(cfg.get("claude_request_timeout", 600.0)),
        live_search=bool(cfg.get("claude_live_search", True)),
        save_reports=bool(cfg.get("claude_save_reports", True)),
        trading=trading,
        trade_amount=float(_cfg(cfg, "ai_trade_amount", "claude_trade_amount", 1000.0)),
        max_positions=int(_cfg(cfg, "ai_max_positions", "claude_max_positions", 5)),
        max_buys_per_poll=int(
            _cfg(cfg, "ai_max_buys_per_poll", "claude_max_buys_per_poll", 3)),
        max_sells_per_poll=int(
            _cfg(cfg, "ai_max_sells_per_poll", "claude_max_sells_per_poll", 5)),
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
        risk_pct=float(_cfg(cfg, "ai_risk_pct", "claude_risk_pct", 1.0)),
        trade_style=_cfg(cfg, "ai_trade_style", "claude_trade_style",
                         "Moderate position"),
        min_reward_risk=float(
            _cfg(cfg, "ai_min_reward_risk", "claude_min_reward_risk", 3.0)),
    )


def main() -> None:
    cfg = load_config()

    if not cfg.get("claude_research_enabled", False):
        print("[ai] claude_research_enabled is false — Anthropic research off.",
              flush=True)
        return

    gs = _build_suggestions(cfg)
    n_hydrated = _hydrate_suggestions(gs)
    positions_poll = float(
        _cfg(cfg, "ai_positions_poll_sec", "claude_positions_poll_sec", 5.0))

    print(f"[ai] anthropic backend={gs.backend} model={gs.model} "
          f"trading={gs.trading} mode={gs.trading_mode}", flush=True)
    print(f"[ai] research times {gs.research_times or '(interval)'} ET — "
          f"next {gs.next_run_label() or 'n/a'}", flush=True)
    if n_hydrated:
        print(f"[ai] hydrated {n_hydrated} idea(s) from {SUGGESTIONS_FILE.name}",
              flush=True)
    if gs.trading and gs.trading_mode == "off":
        print("[ai] WARNING: trading requested but no Alpaca session — "
              "check signal_engine.env", flush=True)

    last_positions_tick = 0.0
    while True:
        t0 = time.time()

        if not gs.refresh(t0):
            gs.refresh_quotes(t0)
            gs.refresh_volume(t0)

        if gs.trading and (t0 - last_positions_tick) >= positions_poll:
            last_positions_tick = t0
            try:
                ai_positions.manage_open_positions(t0)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] manage_open_positions failed: {e}", flush=True)

        _write_json(SUGGESTIONS_FILE, _suggestions_payload(gs, t0))
        pos = _positions_payload(gs.trading_mode, t0)
        _write_json(POSITIONS_FILE, pos)
        _write_json(POSITIONS_FILE_LEGACY, pos)  # one-release alias

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ai] stopped.", flush=True)
