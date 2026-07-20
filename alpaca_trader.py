"""
alpaca_trader.py — Alpaca order execution module

Imported by signal_engine.py.  NOT a standalone script.

Activated by setting TRADER_MODE in signal_engine.env:
  TRADER_MODE=off     — no orders placed (default)
  TRADER_MODE=paper   — paper account (Alpaca paper API, $100k fake money)
  TRADER_MODE=live    — real money (Alpaca live API)

call init() once at startup, then buy() / sell() on each signal.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_HERE        = Path(__file__).parent
_TRADE_LOG   = _HERE / "alpaca_trade_log.json"

# Module-level state — set by init()
_mode:           str   = "off"    # "off" | "paper" | "live"
_trade_amount:   float = 500.0    # dollars per BUY
_extended_hours: bool  = False    # allow pre/post-market orders
_limit_offset:   float = 0.0      # % to push ext-hours limit prices for fills
_stop_loss_pct:  float = 0.0      # % below entry for bracket stop (0 = off)
_take_profit_pct: float = 0.0     # % above entry for bracket TP (0 = off)
_use_brackets:   bool  = False    # attach OTOCO legs on RTH market buys
_client                = None     # alpaca TradingClient instance


# ── Initialisation ────────────────────────────────────────────────────────────

def init(mode: str, api_key: str, secret_key: str, trade_amount: float = 500.0,
         extended_hours: bool = False, limit_offset_pct: float = 0.0,
         stop_loss_pct: float = 0.0, take_profit_pct: float = 0.0,
         use_brackets: bool | None = None):
    """
    Initialise the Alpaca trading module.
    Call once at signal engine startup.

    mode             : "off" | "paper" | "live"
    api_key          : Alpaca API key
    secret_key       : Alpaca secret key
    trade_amount     : dollars to spend per BUY signal
    extended_hours   : allow pre/post-market limit orders
    limit_offset_pct : push ext-hours limit prices this %% past the touch
                       (buys higher, sells lower) to improve fills in thin books
    stop_loss_pct    : if >0 with take_profit and brackets on, attach stop leg
    take_profit_pct  : if >0 with stop_loss and brackets on, attach TP leg
    use_brackets     : attach bracket exits on RTH buys (default: both SL/TP > 0)
    """
    global _mode, _trade_amount, _extended_hours, _limit_offset, _client
    global _stop_loss_pct, _take_profit_pct, _use_brackets

    _mode           = mode.lower().strip()
    _trade_amount   = trade_amount
    _extended_hours = bool(extended_hours)
    _limit_offset   = max(0.0, float(limit_offset_pct))
    _stop_loss_pct  = max(0.0, float(stop_loss_pct or 0.0))
    _take_profit_pct = max(0.0, float(take_profit_pct or 0.0))
    if use_brackets is None:
        _use_brackets = _stop_loss_pct > 0 and _take_profit_pct > 0
    else:
        _use_brackets = bool(use_brackets)

    if _mode == "off":
        log.info("[TRADER] mode=off — no orders will be placed")
        return

    # Validate credentials
    if not api_key or not secret_key:
        log.warning(
            "[TRADER] TRADER_MODE=%s but Alpaca credentials are missing. "
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in signal_engine.env. "
            "Falling back to mode=off.", _mode
        )
        _mode = "off"
        return

    # Import alpaca-py (optional dependency)
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        log.error(
            "[TRADER] alpaca-py is not installed.  Run: pip install alpaca-py\n"
            "         Falling back to mode=off."
        )
        _mode = "off"
        return

    paper   = (_mode == "paper")
    _client = TradingClient(api_key, secret_key, paper=paper)

    # Verify the connection works
    try:
        acct = _client.get_account()
        cash = float(acct.cash)
        bp   = float(acct.buying_power)
        log.info(
            "[TRADER] Alpaca connected  mode=%s  cash=$%,.2f  buying_power=$%,.2f",
            _mode.upper(), cash, bp,
        )
        if _use_brackets and _stop_loss_pct > 0 and _take_profit_pct > 0:
            log.info(
                "[TRADER] brackets ON  SL=%.2f%%  TP=%.2f%%  (RTH market buys only)",
                _stop_loss_pct, _take_profit_pct,
            )
        elif _stop_loss_pct > 0 or _take_profit_pct > 0:
            log.info(
                "[TRADER] brackets OFF — client-side exits only "
                "(set BRACKET_EXITS=on and both STOP_LOSS/TAKE_PROFIT > 0)",
            )
    except Exception as e:
        log.error(
            "[TRADER] Could not connect to Alpaca (%s). "
            "Check your API keys. Falling back to mode=off.", e
        )
        _mode   = "off"
        _client = None


def is_active() -> bool:
    """True if the trader is initialised and will place orders."""
    return _mode != "off" and _client is not None


def market_is_open() -> bool:
    """True if the US market is currently open (Alpaca clock).

    Defaults False on error — a limit order is valid in any session, so when we
    can't tell we prefer the (always-legal) limit path over a market order that
    Alpaca would reject outside regular hours.
    """
    if not is_active():
        return False
    try:
        return bool(_client.get_clock().is_open)
    except Exception:
        return False


# ── Order execution ───────────────────────────────────────────────────────────

def buy(ticker: str, price: float, rsi: float, hist: float) -> dict:
    """
    Place a notional BUY order for TRADE_AMOUNT dollars.
    Alpaca handles fractional shares automatically.
    Always logs the action regardless of mode.

    Returns {"ok": bool, "order_id": str|None, "status": str|None} so the
    caller can later look the order up and rebase to the real fill price.
    """
    est_shares = _trade_amount / price if price > 0 else 0
    mode_tag   = f"[{_mode.upper()}]"

    if not is_active():
        _log_action("BUY_LOGGED", ticker, price, rsi, hist,
                    note="TRADER_MODE=off — no order placed")
        return {"ok": False, "order_id": None, "status": None}

    print(f"\n  [TRADER] {mode_tag} 🟢 BUY  {ticker}  "
          f"${_trade_amount:.0f} notional  ~{est_shares:.2f} shares @ ${price:.2f}")

    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, LimitOrderRequest,
            TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if _extended_hours and price and price > 0:
            # Extended-hours orders must be limit + DAY (Alpaca requirement).
            # Brackets are not supported outside RTH.
            limit_px = round(price * (1 + _limit_offset / 100.0), 2)
            order = _client.submit_order(
                LimitOrderRequest(
                    symbol         = ticker,
                    notional       = round(_trade_amount, 2),
                    side           = OrderSide.BUY,
                    time_in_force  = TimeInForce.DAY,
                    limit_price    = limit_px,
                    extended_hours = True,
                )
            )
        elif (_use_brackets and price and price > 0
              and _stop_loss_pct > 0 and _take_profit_pct > 0):
            # Broker-held OTOCO: entry + take-profit + stop-loss.
            # Qty (not notional) so both exit legs size correctly.
            qty = max(round(_trade_amount / price, 4), 0.0001)
            tp = round(price * (1 + _take_profit_pct / 100.0), 2)
            sl = round(price * (1 - _stop_loss_pct / 100.0), 2)
            # stop limit a tick under stop to reduce gap skips
            sl_limit = round(sl * 0.999, 2)
            order = _client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=tp),
                    stop_loss=StopLossRequest(
                        stop_price=sl, limit_price=sl_limit),
                )
            )
            print(f"  [TRADER] 📐 bracket  TP=${tp:.2f}  SL=${sl:.2f}  "
                  f"qty={qty}")
        else:
            order = _client.submit_order(
                MarketOrderRequest(
                    symbol        = ticker,
                    notional      = round(_trade_amount, 2),
                    side          = OrderSide.BUY,
                    time_in_force = TimeInForce.DAY,
                )
            )
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  BUY order submitted  id={order_id}  status={status}")
        _log_action("BUY", ticker, price, rsi, hist,
                    order_id=order_id, order_status=status,
                    note=("bracket" if _use_brackets and not _extended_hours
                          and _stop_loss_pct > 0 else None))
        return {"ok": True, "order_id": order_id, "status": status}

    except Exception as e:
        print(f"  [TRADER] ❌  BUY order failed: {e}")
        _log_action("BUY_ERROR", ticker, price, rsi, hist, error=str(e))
        return {"ok": False, "order_id": None, "status": "error"}


def buy_limit_at_ask(ticker: str, ask: float, dollar_amount: Optional[float] = None,
                     pad_pct: float = 0.0, rsi: float = 0.0, hist: float = 0.0) -> dict:
    """
    Fixed-dollar BUY sized to WHOLE shares, submitted as a limit at ask*(1+pad%).

    Marketable in regular hours; also valid in extended hours (limit + DAY, no
    brackets). Alpaca limit orders require whole shares, so the actual spend is
    <= dollar_amount. Never overspends: qty is floored against the limit price.

    ask           : current ask price (must be > 0)
    dollar_amount : budget for this buy (defaults to the module TRADE_AMOUNT)
    pad_pct       : percent to lift the limit above the ask (better fill odds)

    Returns {"ok": bool, "order_id": str|None, "status": str|None, "note": str|None}.
    """
    amount   = float(dollar_amount if dollar_amount is not None else _trade_amount)
    mode_tag = f"[{_mode.upper()}]"

    if not is_active():
        _log_action("BUY_LOGGED", ticker, ask, rsi, hist,
                    note="TRADER_MODE=off — no order placed")
        return {"ok": False, "order_id": None, "status": None, "note": None}

    if not ask or ask <= 0:
        _log_action("BUY_SKIPPED", ticker, 0.0, rsi, hist, note="no ask")
        return {"ok": False, "order_id": None, "status": "no_ask", "note": "no ask"}

    limit_px = round(ask * (1 + max(0.0, pad_pct) / 100.0), 2)
    qty      = int(amount // limit_px)      # whole shares affordable at the limit
    if qty < 1:
        note = f"ask ${ask:.2f} > ${amount:.0f} budget"
        _log_action("BUY_SKIPPED", ticker, ask, rsi, hist, note=note)
        return {"ok": False, "order_id": None, "status": "under_budget", "note": note}

    print(f"\n  [TRADER] {mode_tag} 🟢 BUY  {ticker}  "
          f"{qty} sh @ limit ${limit_px:.2f}  (~${qty * limit_px:.0f} of ${amount:.0f})")

    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order = _client.submit_order(
            LimitOrderRequest(
                symbol         = ticker,
                qty            = qty,
                side           = OrderSide.BUY,
                time_in_force  = TimeInForce.DAY,
                limit_price    = limit_px,
                extended_hours = _extended_hours,
            )
        )
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  BUY order submitted  id={order_id}  status={status}")
        _log_action("BUY", ticker, limit_px, rsi, hist,
                    order_id=order_id, order_status=status, qty=qty,
                    note="limit_ask")
        return {"ok": True, "order_id": order_id, "status": status,
                "note": None, "qty": qty, "limit_px": limit_px}

    except Exception as e:
        print(f"  [TRADER] ❌  BUY order failed: {e}")
        _log_action("BUY_ERROR", ticker, limit_px, rsi, hist, error=str(e))
        return {"ok": False, "order_id": None, "status": "error", "note": str(e)}


def buy_market_shares(ticker: str, price: float, dollar_amount: Optional[float] = None,
                      rsi: float = 0.0, hist: float = 0.0) -> dict:
    """
    Fixed-dollar BUY sized to WHOLE shares, submitted as a market order.

    Whole shares only — never notional/fractional. RTH only: market orders are
    rejected in extended hours (use buy_limit_at_ask() off-hours).

    price         : reference price for sizing (pass the ask to avoid overspend)
    dollar_amount : budget (defaults to the module TRADE_AMOUNT)

    Returns {"ok": bool, "order_id": str|None, "status": str|None, "note": str|None, "qty": int}.
    """
    amount   = float(dollar_amount if dollar_amount is not None else _trade_amount)
    mode_tag = f"[{_mode.upper()}]"

    if not is_active():
        _log_action("BUY_LOGGED", ticker, price, rsi, hist,
                    note="TRADER_MODE=off — no order placed")
        return {"ok": False, "order_id": None, "status": None, "note": None}

    if not price or price <= 0:
        _log_action("BUY_SKIPPED", ticker, 0.0, rsi, hist, note="no price")
        return {"ok": False, "order_id": None, "status": "no_price", "note": "no price"}

    qty = int(amount // price)      # whole shares only
    if qty < 1:
        note = f"px ${price:.2f} > ${amount:.0f} budget"
        _log_action("BUY_SKIPPED", ticker, price, rsi, hist, note=note)
        return {"ok": False, "order_id": None, "status": "under_budget", "note": note}

    print(f"\n  [TRADER] {mode_tag} 🟢 BUY  {ticker}  "
          f"{qty} sh market  (~${qty * price:.0f} of ${amount:.0f})")

    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order = _client.submit_order(
            MarketOrderRequest(
                symbol        = ticker,
                qty           = qty,          # integer qty — no notional/fractional
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.DAY,
            )
        )
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  BUY order submitted  id={order_id}  status={status}")
        _log_action("BUY", ticker, price, rsi, hist,
                    order_id=order_id, order_status=status, qty=qty,
                    note="market_shares")
        return {"ok": True, "order_id": order_id, "status": status,
                "note": None, "qty": qty}

    except Exception as e:
        print(f"  [TRADER] ❌  BUY order failed: {e}")
        _log_action("BUY_ERROR", ticker, price, rsi, hist, error=str(e))
        return {"ok": False, "order_id": None, "status": "error", "note": str(e)}


def sell(ticker: str, price: float, rsi: float, hist: float,
         buy_price: Optional[float] = None) -> dict:
    """
    Close the entire Alpaca position for a ticker.
    Uses close_position() — no manual share counting needed.
    Always logs the action regardless of mode.

    Returns {"ok": bool, "order_id": str|None, "status": str|None}.
    """
    pnl_str  = ""
    if buy_price and buy_price > 0:
        pnl_pct = (price - buy_price) / buy_price * 100
        pnl_str = f"  P&L {'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%"

    mode_tag = f"[{_mode.upper()}]"

    if not is_active():
        _log_action("SELL_LOGGED", ticker, price, rsi, hist,
                    buy_price=buy_price,
                    note="TRADER_MODE=off — no order placed")
        return {"ok": False, "order_id": None, "status": None}

    # Confirm we actually hold a position
    try:
        pos      = _client.get_open_position(ticker)
        qty_held = float(pos.qty)
    except Exception:
        print(f"  [TRADER] ℹ️  SELL {ticker}: no open Alpaca position — skipping order")
        _log_action("SELL_SKIPPED", ticker, price, rsi, hist,
                    note="no open Alpaca position")
        return {"ok": False, "order_id": None, "status": "skipped"}

    print(f"\n  [TRADER] {mode_tag} 🔴 SELL {ticker}  "
          f"{qty_held} shares @ ${price:.2f}{pnl_str}")

    try:
        if _extended_hours and price and price > 0:
            # close_position() submits a MARKET order, which Alpaca rejects
            # outside regular hours. Sell the held qty as an ext-hours limit
            # order, nudged below the touch so it fills in thin books.
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums   import OrderSide, TimeInForce
            limit_px = round(price * (1 - _limit_offset / 100.0), 2)
            order = _client.submit_order(
                LimitOrderRequest(
                    symbol         = ticker,
                    qty            = qty_held,
                    side           = OrderSide.SELL,
                    time_in_force  = TimeInForce.DAY,
                    limit_price    = limit_px,
                    extended_hours = True,
                )
            )
        else:
            order = _client.close_position(ticker)
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  SELL order submitted  id={order_id}  status={status}")
        _log_action("SELL", ticker, price, rsi, hist,
                    buy_price=buy_price, qty=qty_held,
                    order_id=order_id, order_status=status)
        return {"ok": True, "order_id": order_id, "status": status}

    except Exception as e:
        print(f"  [TRADER] ❌  SELL order failed: {e}")
        _log_action("SELL_ERROR", ticker, price, rsi, hist,
                    buy_price=buy_price, error=str(e))
        return {"ok": False, "order_id": None, "status": "error"}


def close_out(ticker: str, price: float = 0.0, rsi: float = 0.0, hist: float = 0.0) -> dict:
    """
    Desk EXIT: cancel any OPEN orders for the symbol, then close 100% of the position.

    Cancelling first clears a resting buy limit — otherwise a sell is rejected as a
    wash trade (Alpaca: "buy order exists, sell limit price should be greater...").

    Market open   -> market close_position() (fills 100%).
    Market closed -> extended-hours limit SELL of the held qty at price*(1-pad).

    Returns {"ok", "order_id", "status", "note", "canceled"}.
    """
    if not is_active():
        _log_action("SELL_LOGGED", ticker, price, rsi, hist, note="TRADER_MODE=off")
        return {"ok": False, "order_id": None, "status": None, "note": None, "canceled": 0}

    ticker = ticker.upper()

    # 1) Cancel resting orders for this symbol (esp. an unfilled buy limit).
    canceled = 0
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        open_orders = _client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]))
        for o in open_orders or []:
            try:
                _client.cancel_order_by_id(o.id)
                canceled += 1
            except Exception as e:
                log.warning("[TRADER] cancel %s failed: %s", getattr(o, "id", "?"), e)
    except Exception as e:
        log.warning("[TRADER] list/cancel open orders for %s failed: %s", ticker, e)

    # 2) Do we actually hold anything?
    try:
        pos = _client.get_open_position(ticker)
        qty_held = float(pos.qty)
    except Exception:
        note = f"no position (canceled {canceled} open order{'' if canceled == 1 else 's'})"
        print(f"  [TRADER] ℹ️  {ticker}: {note}")
        _log_action("CANCELED", ticker, price, rsi, hist, canceled=canceled, note=note)
        return {"ok": canceled > 0, "order_id": None,
                "status": "canceled" if canceled else "flat",
                "note": note, "canceled": canceled}

    # 3) Close 100%.
    try:
        if market_is_open():
            order = _client.close_position(ticker)      # market, whole position
        else:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            ref = price if price and price > 0 else float(getattr(pos, "current_price", 0) or 0)
            limit_px = round(ref * (1 - _limit_offset / 100.0), 2) if ref > 0 else 0.0
            if limit_px <= 0:
                return {"ok": False, "order_id": None, "status": "no_price",
                        "note": "off-hours sell needs a price", "canceled": canceled}
            order = _client.submit_order(
                LimitOrderRequest(symbol=ticker, qty=qty_held, side=OrderSide.SELL,
                                  time_in_force=TimeInForce.DAY, limit_price=limit_px,
                                  extended_hours=True))
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  CLOSE {ticker}  {qty_held} sh  id={order_id}  "
              f"status={status}  (canceled {canceled})")
        _log_action("SELL", ticker, price, rsi, hist, qty=qty_held,
                    order_id=order_id, order_status=status, canceled=canceled,
                    note="close_out")
        return {"ok": True, "order_id": order_id, "status": status,
                "note": None, "canceled": canceled}
    except Exception as e:
        print(f"  [TRADER] ❌  CLOSE {ticker} failed: {e}")
        _log_action("SELL_ERROR", ticker, price, rsi, hist, error=str(e), canceled=canceled)
        return {"ok": False, "order_id": None, "status": "error",
                "note": str(e), "canceled": canceled}


# ── Order / position lookups (fill reconciliation) ────────────────────────────

def get_order(order_id: str) -> Optional[dict]:
    """
    Look up one order. Returns {"status", "filled_avg_price", "filled_qty"}
    or None when the trader is off or the lookup fails.
    """
    if not is_active() or not order_id:
        return None
    try:
        o = _client.get_order_by_id(order_id)
        fap = getattr(o, "filled_avg_price", None)
        fq  = getattr(o, "filled_qty", None)
        return {
            "status":           str(o.status),
            "filled_avg_price": float(fap) if fap is not None else None,
            "filled_qty":       float(fq)  if fq  is not None else None,
        }
    except Exception as e:
        log.warning("[TRADER] get_order(%s) failed: %s", order_id, e)
        return None


def get_open_positions() -> Optional[dict]:
    """
    All open Alpaca positions as {symbol: {"qty", "avg_entry_price"}}.
    Returns None when the trader is off or the call fails (so callers can
    tell "no positions" apart from "couldn't ask").
    """
    if not is_active():
        return None
    try:
        out = {}
        for pos in _client.get_all_positions():
            out[str(pos.symbol).upper()] = {
                "qty":             float(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
            }
        return out
    except Exception as e:
        log.warning("[TRADER] get_open_positions failed: %s", e)
        return None


def _f(obj, attr) -> float:
    try:
        v = getattr(obj, attr, 0)
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def get_positions_detail() -> Optional[dict]:
    """
    All open positions with live P&L for display:
      {SYM: {qty, avg_entry, current, pl, plpc, mkt_val}}
    plpc is a PERCENT (Alpaca returns a fraction; this multiplies by 100).
    Returns None when the trader is off or the call fails.
    """
    if not is_active():
        return None
    try:
        out = {}
        for pos in _client.get_all_positions():
            out[str(pos.symbol).upper()] = {
                "qty":       _f(pos, "qty"),
                "avg_entry": _f(pos, "avg_entry_price"),
                "current":   _f(pos, "current_price"),
                "pl":        _f(pos, "unrealized_pl"),
                "plpc":      _f(pos, "unrealized_plpc") * 100.0,
                "mkt_val":   _f(pos, "market_value"),
            }
        return out
    except Exception as e:
        log.warning("[TRADER] get_positions_detail failed: %s", e)
        return None


def place_trailing_stop(ticker: str, trail_percent: float,
                        qty: Optional[float] = None) -> dict:
    """
    Broker-held trailing stop SELL for an open long. Survives engine restarts.

    trail_percent: percent below the high-water mark (e.g. 15.0 = 15%).
    qty: optional; defaults to full open position size.

    Returns {"ok": bool, "order_id": str|None, "status": str|None}.
    """
    if not is_active():
        return {"ok": False, "order_id": None, "status": None}
    if trail_percent is None or float(trail_percent) <= 0:
        return {"ok": False, "order_id": None, "status": "invalid_trail"}

    ticker = ticker.upper()
    try:
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if qty is None:
            pos = _client.get_open_position(ticker)
            qty = float(pos.qty)
        qty = float(qty)
        if qty <= 0:
            return {"ok": False, "order_id": None, "status": "no_qty"}

        order = _client.submit_order(
            TrailingStopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                trail_percent=round(float(trail_percent), 2),
            )
        )
        order_id = str(order.id)
        status = str(order.status)
        print(f"  [TRADER] 📉 trailing stop  {ticker}  trail={trail_percent}%  "
              f"qty={qty}  id={order_id}")
        _log_action("TRAIL_STOP", ticker, 0.0, 0.0, 0.0,
                    order_id=order_id, order_status=status,
                    trail_percent=trail_percent, qty=qty)
        return {"ok": True, "order_id": order_id, "status": status}
    except Exception as e:
        print(f"  [TRADER] ❌  trailing stop failed: {e}")
        _log_action("TRAIL_STOP_ERROR", ticker, 0.0, 0.0, 0.0, error=str(e))
        return {"ok": False, "order_id": None, "status": "error"}


# ── Trade logging ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_action(action: str, ticker: str, price: float,
                rsi: float, hist: float, **kwargs):
    """Append one entry to alpaca_trade_log.json."""
    entry = {
        "action":       action,
        "ticker":       ticker,
        "price":        round(price, 4),
        "rsi":          round(rsi,   2),
        "macd_hist":    round(hist,  6),
        "trader_mode":  _mode,
        "trade_amount": _trade_amount,
        "time":         _now_iso(),
        **kwargs,
    }
    entries = []
    if _TRADE_LOG.exists():
        try:
            entries = json.loads(_TRADE_LOG.read_text())
        except Exception:
            pass
    entries = entries[-999:]   # cap at 1000 total (999 kept + 1 new)
    entries.append(entry)
    _TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=_TRADE_LOG.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        Path(tmp_path).replace(_TRADE_LOG)
        tmp_path = None
    except Exception as e:
        log.error("[TRADER] Failed to write trade log: %s", e)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
