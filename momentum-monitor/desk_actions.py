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
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import desk_core
    desk_core.load_env_file(ROOT / "signal_engine.env")


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
    if _buy_style not in ("auto", "limit_ask", "market", "policy"):
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


def desk_buy(symbol: str, row: dict | None = None) -> str:
    """Buy the focused symbol for $trade_amount. WHOLE shares only — never fractional.

    "auto" (default): MARKET order when the market is open (reliable fill, no
        resting order to block a later sell); LIMIT at ask*(1+pad%) off-hours.
    "limit_ask": always a whole-share limit at ask*(1+pad%).
    "market": always a whole-share market order — RTH only.
    "policy": adaptive limit via entry_pricing (passive/fair/join).
    """
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"BUY blocked — trader mode={_trader_mode} (set TRADER_MODE=paper|live)"
    sym = symbol.upper().strip()

    # Resolve auto → market when open, limit when closed.
    style = _buy_style
    if style == "auto":
        style = "market" if alpaca_trader.market_is_open() else "limit_ask"
    if style == "policy":
        return desk_buy_policy(sym, row=row)

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


def desk_buy_policy(
    symbol: str,
    row: dict | None = None,
    *,
    max_spread_pct: float = 1.0,
    pad_max_pct: float = 0.15,
    rvol_hot: float = 3.0,
    max_price: float | None = None,
) -> str:
    """Adaptive limit buy (no bracket). Prefer desk_buy_bracket for auto path."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"BUY blocked — trader mode={_trader_mode}"

    sym = symbol.upper().strip()
    dec = _price_decision(
        sym, row=row, max_spread_pct=max_spread_pct,
        pad_max_pct=pad_max_pct, rvol_hot=rvol_hot, max_price=max_price,
    )
    if not dec.ok or not dec.limit_px:
        return f"BUY {sym} policy skip — {dec.reason}"

    out = alpaca_trader.buy_limit_at_price(
        sym, dec.limit_px, _trade_amount,
        note=f"policy:{dec.style}",
    )
    if out.get("ok"):
        return (
            f"BUY {sym} {out.get('qty')}sh @ ${out.get('limit_px'):.2f} "
            f"[{dec.style} urg={dec.urgency:.2f}] id={out.get('order_id')}"
        )
    status = out.get("status")
    if status == "under_budget":
        return f"BUY {sym} skipped — {out.get('note')}"
    return f"BUY {sym} failed status={status} {out.get('note') or ''}".strip()


def _price_decision(
    symbol: str,
    row: dict | None = None,
    *,
    max_spread_pct: float = 1.0,
    pad_max_pct: float = 0.15,
    rvol_hot: float = 3.0,
    max_price: float | None = None,
):
    try:
        import entry_pricing as ep
    except ImportError:
        sys.path.insert(0, str(ROOT))
        import entry_pricing as ep

    row = row or {}
    rvol = row.get("rvol")
    if rvol is None and isinstance(row.get("funnel"), dict):
        rvol = row["funnel"].get("rvol")
    sp = row.get("signal_proximity") or {}
    return ep.decide(
        bid=_latest_bid(symbol),
        ask=_latest_ask(symbol),
        last=_latest_price(symbol),
        rvol=rvol,
        proximity_pct=sp.get("proximity_pct") if isinstance(sp, dict) else None,
        max_spread_pct=max_spread_pct,
        pad_pct=_limit_pad_pct,
        pad_max_pct=pad_max_pct,
        rvol_hot=rvol_hot,
        max_price=max_price,
    )


def desk_buy_bracket(
    symbol: str,
    row: dict | None = None,
    *,
    cfg: dict | None = None,
) -> tuple[str, dict | None]:
    """Policy entry + risk-sized OCO bracket (TP top, SL bottom).

    Returns (status_message, plan_dict_or_None). plan_dict is for desk_book.
    """
    import alpaca_trader
    if not alpaca_trader.is_active():
        return f"BUY blocked — trader mode={_trader_mode}", None

    cfg = cfg or {}
    sym = symbol.upper().strip()
    pol = (row or {}).get("_policy") or {}
    max_spread = float(pol.get("max_spread_pct", cfg.get("entry_max_spread_pct", 1.0)))
    pad_max = float(pol.get("pad_max_pct", cfg.get("entry_pad_max_pct", 0.15)))
    rvol_hot = float(pol.get("rvol_hot", cfg.get("rvol_hot", 3.0)))
    max_price = pol.get("max_price", cfg.get("auto_limit_max_price"))
    if max_price is not None:
        try:
            max_price = float(max_price)
        except (TypeError, ValueError):
            max_price = None

    dec = _price_decision(
        sym, row=row, max_spread_pct=max_spread,
        pad_max_pct=pad_max, rvol_hot=rvol_hot, max_price=max_price,
    )
    if not dec.ok or not dec.limit_px:
        return f"BUY {sym} policy skip — {dec.reason}", None

    equity = alpaca_trader.get_equity()
    if not equity or equity <= 0:
        equity = float(cfg.get("fallback_equity", 100_000))

    try:
        import desk_risk as dsk
    except ImportError:
        sys.path.insert(0, str(ROOT))
        import desk_risk as dsk

    plan = dsk.plan_long(
        float(dec.limit_px),
        equity=equity,
        risk_pct=float(cfg.get("risk_pct", 0.35)),
        stop_pct=float(cfg.get("stop_pct", 0.40)),
        reward_r=float(cfg.get("reward_r", 2.0)),
        max_notional=(
            float(cfg["max_notional"])
            if cfg.get("max_notional") is not None else None
        ),
    )
    if plan is None:
        return f"BUY {sym} size skip — risk/stop yields 0 shares", None

    out = alpaca_trader.buy_limit_bracket(
        sym, plan.qty, plan.entry, plan.stop, plan.target,
    )
    if not out.get("ok"):
        # No bracket, no position. The old behaviour fell back to a plain
        # limit buy and told the operator to "set stops manually" — which is
        # not a plan, it is a hope. On 2026-08-06 CELH's bracket was rejected
        # ("bracket orders do not support extended hours trading") and the
        # fallback opened 353 shares / $8.4k — 83% of account equity — with no
        # stop at all. It sat naked for 44 minutes until closed by hand. ALOY
        # and XNDU hit the identical path on 08-04.
        #
        # The size is what makes this unsurvivable rather than untidy:
        # plan_long sizes off a 0.40% stop, so the share count is deliberately
        # large *because* the stop is tight. Dropping the stop keeps the size
        # and removes the only thing that justified it.
        err = out.get("note") or out.get("status") or "unknown"
        return (
            f"BUY {sym} ABORTED — bracket rejected ({err}); "
            f"refusing to open {plan.qty}sh (${plan.notional:,.0f}) unprotected",
            None,
        )

    plan_d = plan.as_dict()
    plan_d["pricing"] = dec.as_dict()
    plan_d["bracket"] = True
    plan_d["buy_order_id"] = out.get("buy_order_id")
    msg = (
        f"BUY {sym} {plan.qty}sh LMT@${plan.entry:.2f} "
        f"SL@${plan.stop:.2f} TP@${plan.target:.2f} "
        f"risk=${plan.risk_dollars:.2f} [{dec.style}] "
        f"id={out.get('buy_order_id')}"
    )
    return msg, plan_d


def replace_protective_stop(symbol: str, stop_price: float) -> dict:
    """Cancel resting sell-stops for symbol and place a new stop at stop_price."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return {"ok": False, "status": "off"}
    sym = symbol.upper().strip()
    old_id = None
    try:
        for o in alpaca_trader.get_open_orders() or []:
            if str(o.get("symbol") or "").upper() != sym:
                continue
            if str(o.get("side") or "").lower() != "sell":
                continue
            typ = str(o.get("type") or "").lower()
            if "stop" in typ or o.get("limit") is None:
                # prefer stop-like; get_open_orders may not expose type clearly
                old_id = o.get("id") or o.get("order_id")
                if old_id:
                    break
    except Exception:
        old_id = None
    return alpaca_trader.replace_stop(
        sym, str(old_id) if old_id else None, stop_price=float(stop_price))


def place_trail(symbol: str, trail_percent: float) -> dict:
    import alpaca_trader
    return alpaca_trader.place_trailing_stop(symbol.upper().strip(), trail_percent)


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
