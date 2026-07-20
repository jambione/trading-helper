"""Cross-platform desk actions for the momentum monitor.

- Load focused symbol into TradingView (mac_agent / windows_agent)
- Publish active_symbol.json for other tools
- Alpaca paper/live buy & sell for the focused symbol
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
ACTIVE_PATH = ROOT / "active_symbol.json"

# ── platform agent (TradingView load) ─────────────────────────────────────────

workflow_add_tv = None
_agent = None
_agent_name = "none"

if sys.platform == "darwin":
    try:
        sys.path.insert(0, str(ROOT))
        import mac_agent as _agent
        workflow_add_tv = _agent.workflow_add_tv
        _agent_name = "mac_agent"
    except Exception:
        _agent = None
elif sys.platform == "win32":
    try:
        sys.path.insert(0, str(ROOT))
        import windows_agent as _agent
        workflow_add_tv = _agent.workflow_add_tv
        _agent_name = "windows_agent"
    except Exception:
        _agent = None


def platform_label() -> str:
    return {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, sys.platform)


def tv_load_available() -> bool:
    return workflow_add_tv is not None and sys.platform in ("darwin", "win32")


def load_tv(symbol: str) -> bool:
    if not workflow_add_tv:
        return False
    return bool(workflow_add_tv(symbol.upper().strip()))


def publish_focus(symbol: str | None, source: str = "momentum-monitor") -> None:
    """Write the focused/loaded symbol for other processes (flow_monitor, etc.)."""
    payload = {
        "symbol": (symbol or "").upper() or None,
        "ts": time.time(),
        "source": source,
        "platform": platform_label(),
    }
    tmp = ACTIVE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(ACTIVE_PATH)


def read_focus() -> Optional[str]:
    try:
        data = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
        sym = data.get("symbol")
        return str(sym).upper() if sym else None
    except Exception:
        return None


# ── Alpaca trader (lazy init from signal_engine.env) ──────────────────────────

_trader_ready = False
_trader_mode = "off"
_trade_amount = 500.0


def _load_env():
    env_path = ROOT / "signal_engine.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().split("#")[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def init_trader(cfg: dict | None = None) -> str:
    """Init alpaca_trader from env. Returns mode string (off/paper/live)."""
    global _trader_ready, _trader_mode, _trade_amount
    _load_env()
    cfg = cfg or {}
    mode = str(cfg.get("trader_mode")
               or os.getenv("TRADER_MODE", "paper")).strip().lower()
    if mode not in ("off", "paper", "live"):
        mode = "paper"
    amount = float(cfg.get("trade_amount")
                   or os.getenv("TRADE_AMOUNT", "500") or 500)
    _trade_amount = amount

    sys.path.insert(0, str(ROOT))
    import alpaca_trader

    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    # Desk never forces brackets unless env already configured them
    be = os.getenv("BRACKET_EXITS", "auto").strip().lower()
    use_brackets = None
    if be in ("0", "false", "off", "no"):
        use_brackets = False
    elif be in ("1", "true", "on", "yes"):
        use_brackets = True

    sl = float(os.getenv("STOP_LOSS", "0") or 0)
    tp = float(os.getenv("TAKE_PROFIT", "0") or 0)
    ext = os.getenv("EXTENDED_HOURS", "false").lower() in ("1", "true", "yes", "on")

    alpaca_trader.init(
        mode=mode,
        api_key=api,
        secret_key=sec,
        trade_amount=amount,
        extended_hours=ext,
        stop_loss_pct=sl,
        take_profit_pct=tp,
        use_brackets=use_brackets,
    )
    _trader_ready = alpaca_trader.is_active()
    _trader_mode = mode if _trader_ready else "off"
    return _trader_mode


def trader_mode() -> str:
    return _trader_mode


def trade_amount() -> float:
    return _trade_amount


def _latest_price(symbol: str) -> Optional[float]:
    """Prefer Alpaca IEX latest trade; fall back to latest quote mid."""
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not api or not sec:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest, StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        client = StockHistoricalDataClient(api, sec)
        try:
            tr = client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
            t = tr.get(symbol) if isinstance(tr, dict) else tr
            if t is not None and float(t.price) > 0:
                return float(t.price)
        except Exception:
            pass
        q = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        quote = q.get(symbol) if isinstance(q, dict) else q
        if quote is None:
            return None
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return bid or ask or None
    except Exception:
        return None


def desk_buy(symbol: str) -> str:
    """Notional buy of focused symbol. Returns short status for the UI."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"BUY blocked — trader mode={_trader_mode} (set TRADER_MODE=paper|live)"
    sym = symbol.upper().strip()
    px = _latest_price(sym)
    if not px or px <= 0:
        return f"BUY {sym} failed — no price (IEX)"
    out = alpaca_trader.buy(sym, price=px, rsi=0.0, hist=0.0)
    if out.get("ok"):
        return f"BUY {sym} ${px:.2f} ~${_trade_amount:.0f} id={out.get('order_id')}"
    return f"BUY {sym} failed status={out.get('status')}"


def desk_sell(symbol: str) -> str:
    """Close entire position for focused symbol."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"SELL blocked — trader mode={_trader_mode}"
    sym = symbol.upper().strip()
    px = _latest_price(sym) or 0.0
    out = alpaca_trader.sell(sym, price=px, rsi=0.0, hist=0.0)
    if out.get("ok"):
        return f"SELL {sym} id={out.get('order_id')}"
    return f"SELL {sym} {out.get('status') or 'failed'}"
