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


def tv_focus_symbol() -> Optional[str]:
    """Current TradingView chart symbol (read from the browser tab), or None.

    macOS only for now (Brave/Chrome via AppleScript). Returns None elsewhere.
    """
    reader = getattr(_agent, "read_tv_symbol", None)
    if not callable(reader):
        return None
    try:
        sym = reader()
        return str(sym).upper().strip() if sym else None
    except Exception:
        return None


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
_buy_style = "auto"           # "auto" (mkt when open, limit off-hours) | "limit_ask" | "market"
_limit_pad_pct = 0.1          # % above ask (buys) / below bid (ext-hours sells)
_extended = False             # allow pre/post-market B/S


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
    global _buy_style, _limit_pad_pct, _extended
    _load_env()
    cfg = cfg or {}
    mode = str(cfg.get("trader_mode")
               or os.getenv("TRADER_MODE", "paper")).strip().lower()
    if mode not in ("off", "paper", "live"):
        mode = "paper"
    amount = float(cfg.get("trade_amount")
                   or os.getenv("TRADE_AMOUNT", "500") or 500)
    _trade_amount = amount

    _buy_style = str(cfg.get("buy_order_style")
                     or os.getenv("BUY_ORDER_STYLE", "auto")).strip().lower()
    if _buy_style not in ("auto", "limit_ask", "market"):
        # Unknown (incl. the retired fractional "notional_market") → safe default
        _buy_style = "auto"
    pad = cfg.get("limit_pad_pct")
    if pad is None:
        pad = os.getenv("LIMIT_PAD_PCT", "0.1") or 0.1
    _limit_pad_pct = max(0.0, float(pad))

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
    # Extended hours: config wins over env (env default off for back-compat)
    ext_cfg = cfg.get("extended_hours")
    if ext_cfg is None:
        ext = os.getenv("EXTENDED_HOURS", "false").lower() in ("1", "true", "yes", "on")
    else:
        ext = bool(ext_cfg)
    _extended = ext

    alpaca_trader.init(
        mode=mode,
        api_key=api,
        secret_key=sec,
        trade_amount=amount,
        extended_hours=ext,
        limit_offset_pct=_limit_pad_pct,   # ext-hours sells nudge below the bid
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


def positions_detail() -> Optional[dict]:
    """Open positions with live P&L for the monitor, or None when off/unavailable."""
    try:
        import alpaca_trader
        return alpaca_trader.get_positions_detail()
    except Exception:
        return None


def buy_style() -> str:
    return _buy_style


def extended_hours() -> bool:
    return _extended


def _data_client():
    """StockHistoricalDataClient from Alpaca env creds, or None."""
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not api or not sec:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(api, sec)
    except Exception:
        return None


def _last_trade(symbol: str) -> Optional[float]:
    """Latest IEX trade price, or None."""
    client = _data_client()
    if client is None:
        return None
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        from alpaca.data.enums import DataFeed
        tr = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        t = tr.get(symbol) if isinstance(tr, dict) else tr
        if t is not None and float(t.price) > 0:
            return float(t.price)
    except Exception:
        pass
    return None


def _bid_ask(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """(bid, ask) from the latest IEX quote; each None when unavailable."""
    client = _data_client()
    if client is None:
        return (None, None)
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        q = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        quote = q.get(symbol) if isinstance(q, dict) else q
        if quote is None:
            return (None, None)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        return (bid or None, ask or None)
    except Exception:
        return (None, None)


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask or None


def _latest_price(symbol: str) -> Optional[float]:
    """Prefer Alpaca IEX latest trade; fall back to latest quote mid."""
    px = _last_trade(symbol)
    if px:
        return px
    return _mid(*_bid_ask(symbol))


def _latest_ask(symbol: str) -> Optional[float]:
    """Current ask; fall back to mid, then last trade (thin off-hours books)."""
    bid, ask = _bid_ask(symbol)
    if ask:
        return ask
    return _mid(bid, ask) or _last_trade(symbol)


def _latest_bid(symbol: str) -> Optional[float]:
    """Current bid; fall back to mid, then last trade (thin off-hours books)."""
    bid, ask = _bid_ask(symbol)
    if bid:
        return bid
    return _mid(bid, ask) or _last_trade(symbol)


def desk_buy(symbol: str) -> str:
    """Buy the focused symbol for $trade_amount. WHOLE shares only — never fractional.

    "auto" (default): MARKET order when the market is open (reliable fill, no
        resting order to block a later sell); LIMIT at ask*(1+pad%) off-hours.
    "limit_ask": always a whole-share limit at ask*(1+pad%) — patient entry.
    "market": always a whole-share market order — RTH only.
    """
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"BUY blocked — trader mode={_trader_mode} (set TRADER_MODE=paper|live)"
    sym = symbol.upper().strip()

    # Resolve auto → market when open, limit when closed.
    style = _buy_style
    if style == "auto":
        style = "market" if alpaca_trader.market_is_open() else "limit_ask"

    ask = _latest_ask(sym)
    if not ask or ask <= 0:
        return f"BUY {sym} — no price (IEX)"

    if style == "market":
        # Size off the ask so we don't overspend; whole shares via buy_market_shares.
        out = alpaca_trader.buy_market_shares(sym, ask, _trade_amount)
        if out.get("ok"):
            return f"BUY {sym} {out.get('qty')}sh mkt id={out.get('order_id')}"
    else:  # limit_ask
        out = alpaca_trader.buy_limit_at_ask(sym, ask, _trade_amount, _limit_pad_pct)
        if out.get("ok"):
            ext = " ext" if _extended else ""
            return (f"BUY {sym} {out.get('qty')}sh @ ${out.get('limit_px'):.2f}{ext} "
                    f"id={out.get('order_id')}")

    status = out.get("status")
    if status == "under_budget":
        return f"BUY {sym} skipped — {out.get('note')}"
    return f"BUY {sym} failed status={status}"


def desk_sell(symbol: str) -> str:
    """Exit the focused symbol: cancel any resting buy order, then close 100%.

    RTH: market close_position(). Off-hours: limit sell of the held qty at
    bid*(1-pad%). Cancelling first avoids the wash-trade rejection a resting
    buy limit would otherwise trigger.
    """
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"SELL blocked — trader mode={_trader_mode}"
    sym = symbol.upper().strip()
    px = _latest_bid(sym) or 0.0
    out = alpaca_trader.close_out(sym, price=px)
    n = out.get("canceled") or 0
    canc = f" (canceled {n})" if n else ""
    if out.get("ok"):
        oid = out.get("order_id")
        if oid:
            return f"SELL {sym} id={oid}{canc}"
        return f"SELL {sym} — {out.get('note')}"          # canceled resting order, no position
    status = out.get("status")
    if status == "flat":
        return f"SELL {sym} — nothing to sell"
    return f"SELL {sym} {status or 'failed'} {out.get('note') or ''}".strip()
