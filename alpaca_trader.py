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
