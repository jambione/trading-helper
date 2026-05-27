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
_client                = None     # alpaca TradingClient instance


# ── Initialisation ────────────────────────────────────────────────────────────

def init(mode: str, api_key: str, secret_key: str, trade_amount: float = 500.0,
         extended_hours: bool = False):
    """
    Initialise the Alpaca trading module.
    Call once at signal engine startup.

    mode           : "off" | "paper" | "live"
    api_key        : Alpaca API key
    secret_key     : Alpaca secret key
    trade_amount   : dollars to spend per BUY signal
    extended_hours : allow pre/post-market limit orders
    """
    global _mode, _trade_amount, _extended_hours, _client

    _mode           = mode.lower().strip()
    _trade_amount   = trade_amount
    _extended_hours = bool(extended_hours)

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

def buy(ticker: str, price: float, rsi: float, hist: float):
    """
    Place a notional BUY order for TRADE_AMOUNT dollars.
    Alpaca handles fractional shares automatically.
    Always logs the action regardless of mode.
    """
    est_shares = _trade_amount / price if price > 0 else 0
    mode_tag   = f"[{_mode.upper()}]"

    if not is_active():
        _log_action("BUY_LOGGED", ticker, price, rsi, hist,
                    note="TRADER_MODE=off — no order placed")
        return

    print(f"\n  [TRADER] {mode_tag} 🟢 BUY  {ticker}  "
          f"${_trade_amount:.0f} notional  ~{est_shares:.2f} shares @ ${price:.2f}")

    try:
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums   import OrderSide, TimeInForce

        if _extended_hours and price and price > 0:
            # Extended-hours orders must be limit + DAY (Alpaca requirement)
            order = _client.submit_order(
                LimitOrderRequest(
                    symbol         = ticker,
                    notional       = round(_trade_amount, 2),
                    side           = OrderSide.BUY,
                    time_in_force  = TimeInForce.DAY,
                    limit_price    = round(price, 2),
                    extended_hours = True,
                )
            )
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
                    order_id=order_id, order_status=status)

    except Exception as e:
        print(f"  [TRADER] ❌  BUY order failed: {e}")
        _log_action("BUY_ERROR", ticker, price, rsi, hist, error=str(e))


def sell(ticker: str, price: float, rsi: float, hist: float,
         buy_price: Optional[float] = None):
    """
    Close the entire Alpaca position for a ticker.
    Uses close_position() — no manual share counting needed.
    Always logs the action regardless of mode.
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
        return

    # Confirm we actually hold a position
    try:
        pos      = _client.get_open_position(ticker)
        qty_held = float(pos.qty)
    except Exception:
        print(f"  [TRADER] ℹ️  SELL {ticker}: no open Alpaca position — skipping order")
        _log_action("SELL_SKIPPED", ticker, price, rsi, hist,
                    note="no open Alpaca position")
        return

    print(f"\n  [TRADER] {mode_tag} 🔴 SELL {ticker}  "
          f"{qty_held} shares @ ${price:.2f}{pnl_str}")

    try:
        order    = _client.close_position(ticker)
        order_id = str(order.id)
        status   = str(order.status)
        print(f"  [TRADER] ✓  SELL order submitted  id={order_id}  status={status}")
        _log_action("SELL", ticker, price, rsi, hist,
                    buy_price=buy_price, qty=qty_held,
                    order_id=order_id, order_status=status)

    except Exception as e:
        print(f"  [TRADER] ❌  SELL order failed: {e}")
        _log_action("SELL_ERROR", ticker, price, rsi, hist,
                    buy_price=buy_price, error=str(e))


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
    entries.append(entry)
    _TRADE_LOG.write_text(json.dumps(entries, indent=2))
