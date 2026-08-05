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
import time
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

    # Verify the connection works — retry a couple times so a transient
    # "unauthorized"/network blip doesn't disable trading for the whole session.
    acct     = None
    last_err = None
    for attempt in range(3):
        try:
            acct = _client.get_account()
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                log.warning("[TRADER] Alpaca connect attempt %d/3 failed (%s); retrying…",
                            attempt + 1, e)
                time.sleep(1.5 * (attempt + 1))

    if acct is None:
        log.error(
            "[TRADER] Could not connect to Alpaca after 3 tries (%s). "
            "Check your API keys. Falling back to mode=off.", last_err
        )
        _mode   = "off"
        _client = None
        return

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


def buy_limit_at_price(
    ticker: str,
    limit_px: float,
    dollar_amount: Optional[float] = None,
    *,
    rsi: float = 0.0,
    hist: float = 0.0,
    note: str = "limit",
) -> dict:
    """Fixed-dollar BUY as whole-share DAY limit at an explicit price.

    Used by entry_pricing policy and by buy_limit_at_ask (ask+pad).
    """
    amount = float(dollar_amount if dollar_amount is not None else _trade_amount)
    mode_tag = f"[{_mode.upper()}]"
    try:
        limit_px = round(float(limit_px), 2)
    except (TypeError, ValueError):
        return {"ok": False, "order_id": None, "status": "bad_limit", "note": "bad limit"}

    if not is_active():
        _log_action("BUY_LOGGED", ticker, limit_px, rsi, hist,
                    note="TRADER_MODE=off — no order placed")
        return {"ok": False, "order_id": None, "status": None, "note": None}

    if limit_px <= 0:
        _log_action("BUY_SKIPPED", ticker, 0.0, rsi, hist, note="bad limit")
        return {"ok": False, "order_id": None, "status": "bad_limit", "note": "bad limit"}

    qty = int(amount // limit_px)
    if qty < 1:
        n = f"limit ${limit_px:.2f} > ${amount:.0f} budget"
        _log_action("BUY_SKIPPED", ticker, limit_px, rsi, hist, note=n)
        return {"ok": False, "order_id": None, "status": "under_budget", "note": n}

    print(f"\n  [TRADER] {mode_tag} 🟢 BUY  {ticker}  "
          f"{qty} sh @ limit ${limit_px:.2f}  (~${qty * limit_px:.0f} of ${amount:.0f})"
          f"  [{note}]")

    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order = _client.submit_order(
            LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_px,
                extended_hours=_extended_hours,
            )
        )
        order_id = str(order.id)
        status = str(order.status)
        print(f"  [TRADER] ✓  BUY order submitted  id={order_id}  status={status}")
        _log_action("BUY", ticker, limit_px, rsi, hist,
                    order_id=order_id, order_status=status, qty=qty, note=note)
        return {"ok": True, "order_id": order_id, "status": status,
                "note": None, "qty": qty, "limit_px": limit_px}
    except Exception as e:
        print(f"  [TRADER] ❌  BUY order failed: {e}")
        _log_action("BUY_ERROR", ticker, limit_px, rsi, hist, error=str(e))
        return {"ok": False, "order_id": None, "status": "error", "note": str(e)}


def buy_limit_at_ask(ticker: str, ask: float, dollar_amount: Optional[float] = None,
                     pad_pct: float = 0.0, rsi: float = 0.0, hist: float = 0.0) -> dict:
    """
    Fixed-dollar BUY sized to WHOLE shares, submitted as a limit at ask*(1+pad%).

    Marketable in regular hours; also valid in extended hours (limit + DAY, no
    brackets). Alpaca limit orders require whole shares, so the actual spend is
    <= dollar_amount. Never overspends: qty is floored against the limit price.
    """
    if not ask or ask <= 0:
        _log_action("BUY_SKIPPED", ticker, 0.0, rsi, hist, note="no ask")
        return {"ok": False, "order_id": None, "status": "no_ask", "note": "no ask"}
    limit_px = round(float(ask) * (1 + max(0.0, float(pad_pct)) / 100.0), 2)
    return buy_limit_at_price(
        ticker, limit_px, dollar_amount, rsi=rsi, hist=hist, note="limit_ask")


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


def cancel_open_orders(ticker: str | None = None) -> dict:
    """Cancel open orders for one symbol, or all symbols when ticker is None.

    Returns {"ok": bool, "canceled": int, "kept": int, "errors": list}.
    """
    if not is_active():
        return {"ok": False, "canceled": 0, "kept": 0, "errors": ["trader off"]}
    canceled = 0
    errors: list[str] = []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        filt = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
        if ticker:
            filt = GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[ticker.upper()], limit=50)
        open_orders = list(_client.get_orders(filter=filt) or [])
    except Exception as e:
        log.warning("[TRADER] list open orders failed: %s", e)
        return {"ok": False, "canceled": 0, "kept": 0, "errors": [str(e)]}

    for o in open_orders:
        try:
            _client.cancel_order_by_id(o.id)
            canceled += 1
        except Exception as e:
            errors.append(f"{getattr(o, 'id', '?')}: {e}")
            log.warning("[TRADER] cancel %s failed: %s", getattr(o, "id", "?"), e)
    if canceled:
        print(f"  [TRADER] canceled {canceled} open order(s)"
              + (f" for {ticker.upper()}" if ticker else ""))
    return {"ok": True, "canceled": canceled, "kept": 0, "errors": errors}


def dedupe_open_orders(keep: str = "newest") -> dict:
    """Leave at most one open order per (symbol, side); cancel the rest.

    keep: "newest" (default) keeps the most recently submitted order.
    Returns {"ok", "canceled", "kept", "by_symbol"}.
    """
    if not is_active():
        return {"ok": False, "canceled": 0, "kept": 0, "by_symbol": {}}
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        open_orders = list(_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)) or [])
    except Exception as e:
        return {"ok": False, "canceled": 0, "kept": 0, "by_symbol": {},
                "errors": [str(e)]}

    # Group by (symbol, side)
    groups: dict[tuple, list] = {}
    for o in open_orders:
        sym = str(getattr(o, "symbol", "") or "").upper()
        side = str(getattr(o, "side", "") or "").split(".")[-1].lower()
        if not sym:
            continue
        groups.setdefault((sym, side), []).append(o)

    canceled = 0
    kept = 0
    by_symbol: dict[str, int] = {}
    for (sym, side), orders in groups.items():
        def _ts(o):
            t = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            try:
                return t.timestamp() if t is not None else 0.0
            except Exception:
                return 0.0
        orders.sort(key=_ts, reverse=(keep == "newest"))
        # keep[0], cancel the rest
        kept += 1
        by_symbol[sym] = by_symbol.get(sym, 0) + 1
        for o in orders[1:]:
            try:
                _client.cancel_order_by_id(o.id)
                canceled += 1
            except Exception as e:
                log.warning("[TRADER] dedupe cancel %s failed: %s",
                            getattr(o, "id", "?"), e)
    if canceled:
        print(f"  [TRADER] deduped open orders: canceled {canceled}, kept {kept}")
    return {"ok": True, "canceled": canceled, "kept": kept, "by_symbol": by_symbol}


def liquidate_all() -> dict:
    """EOD flatten: cancel every open order, then close every open position.

    Returns
    ``{ok, canceled, closed, symbols, errors}`` where ``closed`` is the count
    of successful ``close_out`` calls and ``symbols`` lists those tickers.
    """
    if not is_active():
        return {
            "ok": False, "canceled": 0, "closed": 0,
            "symbols": [], "errors": ["trader off"],
        }
    cancel = cancel_open_orders(None)
    canceled = int(cancel.get("canceled") or 0)
    errors: list[str] = list(cancel.get("errors") or [])
    symbols: list[str] = []
    detail = get_positions_detail()
    if detail is None:
        errors.append("get_positions_detail failed")
        detail = {}
    for sym in sorted(str(s).upper() for s in detail.keys() if s):
        try:
            res = close_out(sym)
            if res.get("ok"):
                symbols.append(sym)
            else:
                errors.append(
                    f"{sym}:{res.get('note') or res.get('status') or 'close_failed'}"
                )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}:{e}")
            log.warning("[TRADER] liquidate_all %s failed: %s", sym, e)
    ok = not errors or bool(symbols) or canceled > 0 or not detail
    print(
        f"  [TRADER] liquidate_all: canceled={canceled} closed={len(symbols)} "
        f"symbols={symbols or '-'} errors={len(errors)}",
        flush=True,
    )
    return {
        "ok": ok,
        "canceled": canceled,
        "closed": len(symbols),
        "symbols": symbols,
        "errors": errors,
    }


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
    canceled = int(cancel_open_orders(ticker).get("canceled") or 0)

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


def get_open_orders(limit: int = 50) -> list[dict]:
    """
    Resting orders as plain dicts: {symbol, side, qty, filled, type, status, limit}.
    Empty list when the trader is off or the call fails — callers render a
    panel from this and an exception would take the whole surface down.
    """
    if not is_active() or _client is None:
        return []
    try:
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            raw = _client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=limit))
        except Exception:
            raw = _client.get_orders() or []
            raw = [o for o in raw
                   if str(getattr(o, "status", "")).lower()
                   in ("new", "accepted", "pending_new", "partially_filled",
                       "orderstatus.accepted", "orderstatus.new")]
        out: list[dict] = []
        seen: set[tuple] = set()
        for o in raw or []:
            try:
                row = {
                    "id":     str(getattr(o, "id", "") or ""),
                    "symbol": str(getattr(o, "symbol", "") or "").upper(),
                    "side":   str(getattr(o, "side", "") or "").split(".")[-1].lower(),
                    "qty":    _f(o, "qty"),
                    "filled": _f(o, "filled_qty"),
                    "type":   str(getattr(o, "type", "") or "").split(".")[-1].lower(),
                    "status": str(getattr(o, "status", "") or "").split(".")[-1].lower(),
                    "limit":  _f(o, "limit_price") or None,
                    "stop":   _f(o, "stop_price") or None,
                }
                key = (row["symbol"], row["side"], row["qty"], row["limit"],
                       row["status"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
            except Exception:
                continue
        return out
    except Exception as e:
        log.warning("[TRADER] get_open_orders failed: %s", e)
        return []


def get_filled_orders(limit: int = 200, *, days: int | None = 30) -> list[dict]:
    """Closed/filled orders as plain dicts for fill-truth reporting.

    Returns rows with symbol, side, qty, filled_qty, filled_avg_price,
    status, type, submitted_at, filled_at, id, client_order_id.
    Empty list when trader is off or the API call fails.
    """
    if not is_active() or _client is None:
        return []
    try:
        from datetime import timedelta
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        after = None
        if days is not None and days > 0:
            after = datetime.now(timezone.utc) - timedelta(days=int(days))
        kwargs: dict = {
            "status": QueryOrderStatus.CLOSED,
            "limit": max(1, min(int(limit), 500)),
            "nested": True,
        }
        if after is not None:
            kwargs["after"] = after
        raw = _client.get_orders(filter=GetOrdersRequest(**kwargs)) or []
        out: list[dict] = []
        for o in raw:
            try:
                status = str(getattr(o, "status", "") or "").split(".")[-1].lower()
                filled_qty = _f(o, "filled_qty")
                if filled_qty <= 0 and status not in ("filled", "partially_filled"):
                    continue
                fap = getattr(o, "filled_avg_price", None)
                out.append({
                    "id": str(getattr(o, "id", "") or ""),
                    "client_order_id": str(
                        getattr(o, "client_order_id", "") or ""),
                    "symbol": str(getattr(o, "symbol", "") or "").upper(),
                    "side": str(getattr(o, "side", "") or "").split(".")[-1].lower(),
                    "qty": _f(o, "qty"),
                    "filled_qty": filled_qty,
                    "filled_avg_price": float(fap) if fap is not None else None,
                    "type": str(getattr(o, "type", "") or "").split(".")[-1].lower(),
                    "status": status,
                    "submitted_at": str(getattr(o, "submitted_at", "") or ""),
                    "filled_at": str(getattr(o, "filled_at", "") or ""),
                    "order_class": str(
                        getattr(o, "order_class", "") or "").split(".")[-1].lower(),
                })
            except Exception:
                continue
        return out
    except Exception as e:
        log.warning("[TRADER] get_filled_orders failed: %s", e)
        return []


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


def size_by_risk(equity: float, risk_pct: float, entry: float, stop: float) -> int:
    """Whole shares such that a stop fill loses no more than risk_pct of equity.

    ``entry`` and ``stop`` must bracket a real loss (entry > stop for a long);
    anything else has no defined risk to size against, so this returns 0
    rather than guessing a share count from a malformed price pair.
    """
    if equity <= 0 or entry <= 0 or stop <= 0 or stop >= entry:
        return 0
    risk_dollars = equity * (max(0.0, risk_pct) / 100.0)
    per_share_risk = entry - stop
    return max(0, int(risk_dollars // per_share_risk))


def get_equity() -> float | None:
    """Account equity as float, or None if trader off / error."""
    if not is_active() or _client is None:
        return None
    try:
        acct = _client.get_account()
        return float(getattr(acct, "equity", 0) or 0) or None
    except Exception as e:
        log.warning("[TRADER] get_equity failed: %s", e)
        return None


def get_account_day_pl() -> dict | None:
    """Account snapshot for day P&L (Alpaca equity vs last_equity).

    Returns ``{equity, last_equity, day_pl, day_pl_pct}`` or None when the
    trader is off / the call fails. ``day_pl`` is today's change in account
    value (open + closed) in dollars; ``day_pl_pct`` is percent of last_equity.
    """
    if not is_active() or _client is None:
        return None
    try:
        acct = _client.get_account()
        equity = float(getattr(acct, "equity", 0) or 0)
        last_eq = float(getattr(acct, "last_equity", 0) or 0)
        day_pl = equity - last_eq if last_eq > 0 or equity > 0 else 0.0
        day_pl_pct = (day_pl / last_eq * 100.0) if last_eq > 0 else None
        return {
            "equity": equity,
            "last_equity": last_eq,
            "day_pl": day_pl,
            "day_pl_pct": day_pl_pct,
            "cash": float(getattr(acct, "cash", 0) or 0),
            "buying_power": float(getattr(acct, "buying_power", 0) or 0),
        }
    except Exception as e:
        log.warning("[TRADER] get_account_day_pl failed: %s", e)
        return None


def buy_limit_bracket(
    ticker: str,
    qty: float,
    limit_price: float,
    stop_price: float,
    target_price: float,
) -> dict:
    """DAY limit BUY with OTOCO bracket (TP limit + stop-limit SL).

    RTH-oriented. Extended hours + full brackets are restricted on Alpaca;
    when extended_hours is on we still submit and let the API reject if needed.
    """
    ticker = ticker.upper()
    qty = float(qty)
    if (
        not is_active()
        or qty < 1
        or limit_price is None
        or stop_price is None
        or target_price is None
        or float(limit_price) <= 0
        or float(stop_price) <= 0
        or float(target_price) <= float(limit_price)
        or float(stop_price) >= float(limit_price)
    ):
        return {
            "ok": False, "buy_order_id": None, "status": "bad_params",
            "note": "need qty>=1, stop < limit < target",
        }

    try:
        from alpaca.trading.requests import (
            LimitOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        lim = round(float(limit_price), 2)
        sl = round(float(stop_price), 2)
        tp = round(float(target_price), 2)
        sl_limit = round(sl * 0.999, 2)
        order = _client.submit_order(
            LimitOrderRequest(
                symbol=ticker,
                qty=int(qty),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=lim,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp),
                stop_loss=StopLossRequest(stop_price=sl, limit_price=sl_limit),
                extended_hours=_extended_hours,
            )
        )
        print(
            f"  [TRADER] 📐 limit+bracket  {ticker}  qty={int(qty)}  "
            f"LMT=${lim:.2f}  TP=${tp:.2f}  SL=${sl:.2f}"
        )
        _log_action(
            "BUY", ticker, lim, 0.0, 0.0,
            order_id=str(order.id), order_status=str(order.status),
            note=f"limit_bracket tp={tp} sl={sl} qty={int(qty)}",
        )
        return {
            "ok": True,
            "buy_order_id": str(order.id),
            "status": str(order.status),
            "qty": int(qty),
            "limit_px": lim,
            "stop_px": sl,
            "target_px": tp,
            "note": None,
        }
    except Exception as e:
        print(f"  [TRADER] ❌  limit+bracket failed: {e}")
        _log_action("BUY_ERROR", ticker, float(limit_price), 0.0, 0.0, error=str(e))
        return {
            "ok": False, "buy_order_id": None, "status": "error", "note": str(e),
        }


def buy_bracket_exact(ticker: str, qty: float, stop_price: float,
                      target_price: Optional[float] = None) -> dict:
    """RTH market BUY for an exact share count, with exact stop/target prices.

    Unlike ``buy()``, this takes broker-ready prices computed by the caller
    (e.g. from a risk-sized entry plan) rather than the module's global
    percent-based bracket settings — the two paths don't interact.

    With ``target_price``: a single OTOCO bracket order (TP + SL), same shape
    as a scale-out tranche that closes itself at the first target.
    Without it: a plain market buy plus a standalone stop-loss sell for the
    same qty — the SL-only tranche that's meant to ride and later have its
    stop replaced (moved to breakeven, or swapped for a trailing stop).

    Returns {"ok", "buy_order_id", "stop_order_id", "status"}.
    """
    ticker = ticker.upper()
    qty = float(qty)
    if not is_active() or qty <= 0 or stop_price is None or stop_price <= 0:
        return {"ok": False, "buy_order_id": None, "stop_order_id": None,
                "status": None}

    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, StopOrderRequest,
            TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        sl = round(float(stop_price), 2)
        sl_limit = round(sl * 0.999, 2)

        if target_price is not None and float(target_price) > 0:
            tp = round(float(target_price), 2)
            order = _client.submit_order(
                MarketOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=tp),
                    stop_loss=StopLossRequest(stop_price=sl, limit_price=sl_limit),
                )
            )
            print(f"  [TRADER] 📐 bracket(exact)  {ticker}  qty={qty}  "
                  f"TP=${tp:.2f}  SL=${sl:.2f}")
            _log_action("BUY", ticker, tp, 0.0, 0.0,
                        order_id=str(order.id), order_status=str(order.status),
                        note=f"bracket_exact tp={tp} sl={sl}")
            return {"ok": True, "buy_order_id": str(order.id),
                    "stop_order_id": None, "status": str(order.status)}

        # No target — plain buy, then a standalone stop sell for the same qty.
        buy_order = _client.submit_order(
            MarketOrderRequest(
                symbol=ticker, qty=qty,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            )
        )
        stop_order = _client.submit_order(
            StopOrderRequest(
                symbol=ticker, qty=qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                stop_price=sl,
            )
        )
        print(f"  [TRADER] 📐 buy+stop(exact)  {ticker}  qty={qty}  SL=${sl:.2f}")
        _log_action("BUY", ticker, sl, 0.0, 0.0,
                    order_id=str(buy_order.id), order_status=str(buy_order.status),
                    note=f"buy_plus_stop sl={sl} stop_order={stop_order.id}")
        return {"ok": True, "buy_order_id": str(buy_order.id),
                "stop_order_id": str(stop_order.id),
                "status": str(buy_order.status)}
    except Exception as e:
        print(f"  [TRADER] ❌  bracket(exact) failed: {e}")
        _log_action("BUY_ERROR", ticker, 0.0, 0.0, 0.0, error=str(e))
        return {
            "ok": False, "buy_order_id": None, "stop_order_id": None,
            "status": "error", "note": str(e), "error": str(e),
        }


def replace_stop(ticker: str, old_stop_order_id: Optional[str],
                 *, trail_percent: Optional[float] = None,
                 stop_price: Optional[float] = None) -> dict:
    """Cancel a resting protective order and replace it for the same symbol.

    Used to move a tranche's stop to breakeven, or swap it for a trailing
    stop, once its sibling tranche's target fills. Sizes to the full
    remaining open position — correct at that point, since the sibling
    tranche's shares are already gone from the merged Alpaca position.
    """
    if not is_active():
        return {"ok": False, "order_id": None, "status": None}
    ticker = ticker.upper()
    if old_stop_order_id:
        try:
            _client.cancel_order_by_id(old_stop_order_id)
        except Exception:
            pass  # already filled/canceled — fine, we still replace below
    if trail_percent is not None and float(trail_percent) > 0:
        return place_trailing_stop(ticker, float(trail_percent))
    if stop_price is not None and float(stop_price) > 0:
        try:
            from alpaca.trading.requests import StopOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            pos = _client.get_open_position(ticker)
            qty = float(pos.qty)
            if qty <= 0:
                return {"ok": False, "order_id": None, "status": "no_qty"}
            order = _client.submit_order(
                StopOrderRequest(
                    symbol=ticker, qty=qty,
                    side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                    stop_price=round(float(stop_price), 2),
                )
            )
            return {"ok": True, "order_id": str(order.id), "status": str(order.status)}
        except Exception as e:
            return {"ok": False, "order_id": None, "status": "error", "error": str(e)}
    return {"ok": False, "order_id": None, "status": "no_replacement_given"}


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
