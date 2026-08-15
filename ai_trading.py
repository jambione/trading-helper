"""Paper-only Alpaca trading tools for Claude research.

Claude never gets live keys through this module. Even if TRADER_MODE=live in
signal_engine.env, Claude's session is forced to paper. Orders use the same
whole-share sizing path as the desk hotkeys.

Tools exposed to the model (function calling):
  buy_stock, sell_stock, list_positions, get_account
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")

# Module state — set by init_for_ai()
_ready = False
_mode = "off"  # always "paper" when ready, else "off"
_trade_amount = 1000.0
_max_positions = 5
_slot_equity = 250.0
_max_position_pct = 8.0
_max_buys_per_poll = 3
_max_sells_per_poll = 5
_min_score_to_buy = 7.0  # soft guidance only — model decides; hard caps below
_buys_this_poll = 0
_sells_this_poll = 0
_trade_log: list[dict[str, Any]] = []
_lock = threading.Lock()

from ai_paths import resolve_report_dir  # noqa: E402

TRADE_LOG_PATH = resolve_report_dir() / "trades.jsonl"


import desk_core  # noqa: E402

_load_env = desk_core.load_desk_env


def init_for_ai(
    *,
    trade_amount: float = 1000.0,
    max_positions: int = 5,
    max_buys_per_poll: int = 3,
    max_sells_per_poll: int = 5,
    min_score_to_buy: float = 7.0,
    extended_hours: bool = True,
    slot_equity: float = 250.0,
    max_position_pct: float = 8.0,
) -> str:
    """Init Alpaca in PAPER only for the AI desk. Returns 'paper' or 'off'."""
    global _ready, _mode, _trade_amount, _max_positions
    global _slot_equity, _max_position_pct
    global _max_buys_per_poll, _max_sells_per_poll, _min_score_to_buy
    global _buys_this_poll, _sells_this_poll, TRADE_LOG_PATH

    _load_env()
    try:
        TRADE_LOG_PATH = resolve_report_dir() / "trades.jsonl"
    except Exception:
        pass
    _trade_amount = max(1.0, float(trade_amount))
    _max_positions = max(1, int(max_positions))
    try:
        _slot_equity = float(slot_equity)
    except (TypeError, ValueError):
        _slot_equity = 250.0
    try:
        _max_position_pct = max(0.0, float(max_position_pct))
    except (TypeError, ValueError):
        _max_position_pct = 8.0
    _max_buys_per_poll = max(0, int(max_buys_per_poll))
    _max_sells_per_poll = max(0, int(max_sells_per_poll))
    _min_score_to_buy = float(min_score_to_buy)
    _buys_this_poll = 0
    _sells_this_poll = 0

    api = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api or not sec:
        _ready = False
        _mode = "off"
        return "off"

    import alpaca_trader

    # HARD RULE: AI desk path never places live orders through this module.
    alpaca_trader.init(
        mode="paper",
        api_key=api,
        secret_key=sec,
        trade_amount=_trade_amount,
        extended_hours=bool(extended_hours),
        limit_offset_pct=float(os.getenv("LIMIT_PAD_PCT", "0.1") or 0.1),
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        use_brackets=False,
    )
    _ready = alpaca_trader.is_active()
    _mode = "paper" if _ready else "off"
    # On every session start, collapse stacked open orders so the book
    # never carries multiple buys for the same name from prior polls.
    if _ready:
        try:
            cleanup_duplicate_orders()
        except Exception:
            pass
    return _mode


def init_for_claude(**kwargs) -> str:
    """Back-compat alias — use ``init_for_ai``."""
    return init_for_ai(**kwargs)


def reset_poll_counters() -> None:
    global _buys_this_poll, _sells_this_poll
    with _lock:
        _buys_this_poll = 0
        _sells_this_poll = 0


def is_ready() -> bool:
    return _ready and _mode == "paper"


def mode() -> str:
    return _mode


def trade_amount() -> float:
    return _trade_amount


def recent_trades(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        return list(_trade_log[-limit:])


def _append_log(entry: dict[str, Any]) -> None:
    try:
        from learn_stamps import merge_regime
        entry = merge_regime(entry if isinstance(entry, dict) else {})
    except Exception:
        pass
    with _lock:
        _trade_log.append(entry)
        if len(_trade_log) > 200:
            del _trade_log[:-200]
    try:
        TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRADE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _norm_sym(symbol: str) -> str | None:
    sym = (symbol or "").upper().strip().lstrip("$").split()[0] if symbol else ""
    if not sym or not _TICKER_RE.match(sym) or len(sym) > 6:
        return None
    return sym


def _open_position_count() -> int:
    import alpaca_trader
    detail = alpaca_trader.get_positions_detail() or {}
    return len(detail)


# One multi-symbol quote fetch per poll, shared by every _latest_ask/_latest_bid
# call in that poll. The per-symbol path below is two REST round trips per name
# (ask, then bid) and the watch loop runs it over the whole book every 20s — 11
# names was ~66 calls/min against a 200/min account limit shared with the
# engine, the screeners and the dashboard, and on 2026-08-10 it spent the whole
# session in "too many requests". Alpaca takes a symbol LIST on the same
# endpoint for the same cost as one symbol, so the batch is free.
#
# TTL is deliberately short. This is a cache for one poll's fan-out, not a
# quote store: arming reads the ask to decide whether price is in the entry
# zone, and a stale ask arms on a price the order cannot get.
_QUOTE_TTL_SEC = 3.0
_quote_cache: dict[str, tuple[float, float | None, float | None]] = {}


def prime_quotes(symbols: list[str]) -> int:
    """Fetch ask+bid for many symbols in one call. Returns symbols cached.

    Best-effort: on any failure the callers fall through to their per-symbol
    path, so a batch failure degrades to the old behaviour rather than leaving
    the book without quotes.
    """
    wanted = []
    seen = set()
    for s in symbols or []:
        sym = _norm_sym(s)
        if sym and sym not in seen:
            seen.add(sym)
            wanted.append(sym)
    if not wanted:
        return 0
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not api or not sec:
        return 0
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        client = StockHistoricalDataClient(api, sec)
        q = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=wanted, feed=DataFeed.IEX))
    except Exception:
        return 0
    if not isinstance(q, dict):
        return 0
    now = time.time()
    n = 0
    for sym in wanted:
        quote = q.get(sym)
        if quote is None:
            continue
        try:
            ask = float(getattr(quote, "ask_price", 0) or 0) or None
            bid = float(getattr(quote, "bid_price", 0) or 0) or None
        except (TypeError, ValueError):
            continue
        if ask is None and bid is None:
            continue
        _quote_cache[sym] = (now, ask, bid)
        n += 1
    return n


def _cached_quote(symbol: str) -> tuple[float | None, float | None] | None:
    """(ask, bid) from the last prime_quotes, or None when absent/stale."""
    sym = _norm_sym(symbol)
    if not sym:
        return None
    got = _quote_cache.get(sym)
    if got is None:
        return None
    ts, ask, bid = got
    if time.time() - ts > _QUOTE_TTL_SEC:
        _quote_cache.pop(sym, None)
        return None
    return ask, bid


def _latest_ask(symbol: str) -> float | None:
    hit = _cached_quote(symbol)
    if hit is not None and hit[0] is not None:
        return hit[0]
    try:
        import desk_actions as da
        return da._latest_ask(symbol)
    except Exception:
        pass
    # Fallback without desk_actions import side effects
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
            q = client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
            quote = q.get(symbol) if isinstance(q, dict) else q
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if ask > 0:
                return ask
        except Exception:
            pass
        tr = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        t = tr.get(symbol) if isinstance(tr, dict) else tr
        px = float(getattr(t, "price", 0) or 0)
        return px if px > 0 else None
    except Exception:
        return None


def _latest_bid(symbol: str) -> float | None:
    hit = _cached_quote(symbol)
    if hit is not None and hit[1] is not None:
        return hit[1]
    try:
        import desk_actions as da
        return da._latest_bid(symbol)
    except Exception:
        return _latest_ask(symbol)


def _has_open_position(symbol: str) -> bool:
    import alpaca_trader
    if not alpaca_trader.is_active():
        return False
    try:
        client = getattr(alpaca_trader, "_client", None)
        if client is None:
            return False
        client.get_open_position(symbol.upper())
        return True
    except Exception:
        return False


def has_open_position(symbol: str) -> bool:
    return _has_open_position(symbol)


def open_position_count() -> int:
    return _open_position_count()


def _live_equity() -> float | None:
    try:
        import alpaca_trader
        if not alpaca_trader.is_active():
            return None
        eq = alpaca_trader.get_equity()
        return float(eq) if eq is not None and float(eq) > 0 else None
    except Exception:
        return None


def _book_limits(equity: float | None = None):
    from desk_risk import equity_book_limits
    eq = equity if equity is not None else _live_equity()
    if eq is None:
        return None
    return equity_book_limits(
        float(eq),
        max_positions=_max_positions,
        max_position_pct=_max_position_pct,
        slot_equity=_slot_equity,
    )


def effective_max_positions(equity: float | None = None) -> int:
    """Slot count from live equity; configured ceiling if equity is unknown."""
    lim = _book_limits(equity)
    return int(lim.max_positions) if lim is not None else _max_positions


def can_open_new_position(symbol: str) -> bool:
    return _open_position_count() < effective_max_positions() or _has_open_position(symbol)


def market_is_open() -> bool:
    import alpaca_trader
    return alpaca_trader.market_is_open()


def buys_left_this_poll() -> int:
    return max(0, _max_buys_per_poll - _buys_this_poll)


def record_external_buy(symbol: str, extra: dict[str, Any] | None = None) -> None:
    """Register a buy placed outside ``buy_stock`` against this poll's cap.

    ``claude_positions`` executes its risk-sized entries directly against
    ``alpaca_trader`` (exact broker-side brackets, not the fixed-dollar path
    ``buy_stock`` uses) — this keeps that flow counted against the same
    per-poll buy cap and trade log rather than bypassing them.
    """
    global _buys_this_poll
    with _lock:
        _buys_this_poll += 1
    entry = {"action": "buy", "symbol": symbol.upper(), "mode": "paper",
             "ts": time.time()}
    entry.update(extra or {})
    _append_log(entry)


def cancel_open_for(symbol: str) -> int:
    """Cancel every open order for symbol. Returns how many canceled."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return 0
    try:
        return int(alpaca_trader.cancel_open_orders(symbol).get("canceled") or 0)
    except Exception:
        return 0


def cleanup_duplicate_orders() -> dict[str, Any]:
    """Keep at most one open order per symbol+side; cancel extras."""
    import alpaca_trader
    if not alpaca_trader.is_active():
        return {"ok": False, "canceled": 0, "kept": 0}
    try:
        return alpaca_trader.dedupe_open_orders(keep="newest")
    except Exception as e:
        return {"ok": False, "canceled": 0, "kept": 0, "error": str(e)[:160]}


def buy_stock(
    symbol: str,
    dollar_amount: float | None = None,
    reason: str = "",
    score: float | None = None,
) -> dict[str, Any]:
    """Paper BUY whole shares for ~dollar_amount (default TRADE_AMOUNT).

    Never stacks open orders: any existing open order for the symbol is canceled
    first, then a single new order is placed. If we already hold the symbol,
    open buy orders are cleared and no new buy is submitted.
    """
    global _buys_this_poll
    if not is_ready():
        return {
            "ok": False,
            "error": (
                "AI trading off — need ALPACA paper keys in config/secrets.json "
                "(or signal_engine.env), alpaca-py installed in .venv, and "
                "ai_trading_source=grok|claude with matching *_trading_enabled"
            ),
        }

    sym = _norm_sym(symbol)
    if not sym:
        return {"ok": False, "error": f"invalid symbol: {symbol!r}"}

    with _lock:
        if _buys_this_poll >= _max_buys_per_poll:
            return {
                "ok": False,
                "error": f"buy cap for this research poll ({_max_buys_per_poll})",
            }

    pos_cap = effective_max_positions()
    if _open_position_count() >= pos_cap and not _has_open_position(sym):
        return {
            "ok": False,
            "error": f"max open positions ({pos_cap}) already held",
        }

    import alpaca_trader

    # Always clear resting orders for this symbol before (re)placing.
    canceled = cancel_open_for(sym)

    # Already in the position — don't open a second buy; just cleaned resting.
    if _has_open_position(sym):
        result = {
            "ok": False,
            "action": "buy",
            "symbol": sym,
            "mode": "paper",
            "canceled": canceled,
            "error": "already held — canceled resting orders, skipped new buy",
            "reason": (reason or "")[:120],
            "score": score,
            "ts": time.time(),
        }
        _append_log(result)
        return result

    lim = _book_limits()
    if dollar_amount is not None:
        amount = float(dollar_amount)
    elif lim is not None and lim.dollar_cap > 0:
        amount = float(lim.dollar_cap)
    else:
        amount = _trade_amount
    if lim is not None and lim.dollar_cap > 0:
        amount = min(amount, float(lim.dollar_cap))
    amount = max(1.0, amount)

    ask = _latest_ask(sym)
    if not ask or ask <= 0:
        return {"ok": False, "error": f"no IEX price for {sym}", "canceled": canceled}

    # Brief pause so Alpaca releases buying power after cancel.
    if canceled:
        time.sleep(0.35)

    style = "market" if alpaca_trader.market_is_open() else "limit_ask"
    if style == "market":
        out = alpaca_trader.buy_market_shares(sym, ask, amount, rsi=float(score or 0), hist=0.0)
    else:
        out = alpaca_trader.buy_limit_at_ask(sym, ask, amount, pad_pct=0.1,
                                              rsi=float(score or 0), hist=0.0)

    result = {
        "ok": bool(out.get("ok")),
        "action": "buy",
        "symbol": sym,
        "mode": "paper",
        "style": style,
        "dollar_amount": amount,
        "qty": out.get("qty"),
        "order_id": out.get("order_id"),
        "status": out.get("status"),
        "note": out.get("note"),
        "canceled": canceled,
        "reason": (reason or "")[:120],
        "score": score,
        "ts": time.time(),
    }
    if out.get("ok"):
        with _lock:
            _buys_this_poll += 1
        if canceled:
            result["note"] = (
                (result.get("note") or "") + f" (replaced {canceled} open)"
            ).strip()
    else:
        result["error"] = out.get("note") or out.get("status") or "order failed"
    _append_log(result)
    return result


def sell_stock(symbol: str, reason: str = "") -> dict[str, Any]:
    """Paper SELL — close entire position for symbol."""
    global _sells_this_poll
    if not is_ready():
        return {"ok": False, "error": "AI trading off"}

    sym = _norm_sym(symbol)
    if not sym:
        return {"ok": False, "error": f"invalid symbol: {symbol!r}"}

    with _lock:
        if _sells_this_poll >= _max_sells_per_poll:
            return {
                "ok": False,
                "error": f"sell cap for this research poll ({_max_sells_per_poll})",
            }

    import alpaca_trader

    # Prefer close_out if available (cancels resting buys first).
    px = _latest_bid(sym) or 0.0
    if hasattr(alpaca_trader, "close_out"):
        out = alpaca_trader.close_out(sym, price=px)
    else:
        out = alpaca_trader.sell(sym, px or 0.01, rsi=0.0, hist=0.0)

    result = {
        "ok": bool(out.get("ok")),
        "action": "sell",
        "symbol": sym,
        "mode": "paper",
        "order_id": out.get("order_id"),
        "status": out.get("status"),
        "note": out.get("note"),
        "reason": (reason or "")[:120],
        "ts": time.time(),
    }
    if out.get("ok"):
        with _lock:
            _sells_this_poll += 1
    else:
        result["error"] = out.get("note") or out.get("status") or "sell failed"
    _append_log(result)
    return result


def list_positions() -> dict[str, Any]:
    if not is_ready():
        return {"ok": False, "error": "AI trading off", "positions": []}
    import alpaca_trader
    detail = alpaca_trader.get_positions_detail() or {}
    rows = []
    for sym, p in detail.items():
        rows.append({
            "symbol": sym,
            "qty": p.get("qty"),
            "avg_entry": p.get("avg_entry"),
            "current": p.get("current"),
            "pl": p.get("pl"),
            "plpc": p.get("plpc"),
            "mkt_val": p.get("mkt_val"),
        })
    return {"ok": True, "mode": "paper", "count": len(rows), "positions": rows}


def get_account() -> dict[str, Any]:
    if not is_ready():
        return {"ok": False, "error": "AI trading off"}
    import alpaca_trader
    if not alpaca_trader.is_active():
        return {"ok": False, "error": "trader inactive"}
    try:
        # Access module private client carefully
        client = getattr(alpaca_trader, "_client", None)
        if client is None:
            return {"ok": False, "error": "no client"}
        acct = client.get_account()
        return {
            "ok": True,
            "mode": "paper",
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "equity": float(acct.equity),
            "portfolio_value": float(acct.portfolio_value),
            "status": str(acct.status),
            "trade_amount_default": _trade_amount,
            "max_positions": effective_max_positions(float(acct.equity)),
            "max_positions_ceiling": _max_positions,
            "buys_left_this_poll": max(0, _max_buys_per_poll - _buys_this_poll),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def execute_tool(name: str, arguments: dict[str, Any] | str | None) -> dict[str, Any]:
    """Dispatch a function call from the model."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {"ok": False, "error": f"bad arguments JSON: {arguments[:120]!r}"}
    args = arguments if isinstance(arguments, dict) else {}

    if name == "buy_stock":
        return buy_stock(
            symbol=str(args.get("symbol") or ""),
            dollar_amount=_f(args.get("dollar_amount")),
            reason=str(args.get("reason") or ""),
            score=_f(args.get("score")),
        )
    if name == "sell_stock":
        return sell_stock(
            symbol=str(args.get("symbol") or ""),
            reason=str(args.get("reason") or ""),
        )
    if name == "list_positions":
        return list_positions()
    if name == "get_account":
        return get_account()
    return {"ok": False, "error": f"unknown tool: {name}"}


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tool_definitions() -> list[dict[str, Any]]:
    """OpenAI/xAI function tool schemas for the Responses API."""
    return [
        {
            "type": "function",
            "name": "get_account",
            "description": (
                "Get Alpaca PAPER account cash, buying power, equity, and "
                "how many buys remain this research cycle."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "type": "function",
            "name": "list_positions",
            "description": "List open Alpaca PAPER positions with live P&L.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "type": "function",
            "name": "buy_stock",
            "description": (
                "Place a PAPER buy for a US equity after research. Whole shares "
                f"only. Default size ~${_trade_amount:.0f}. Market when RTH is open, "
                "limit at ask off-hours. Use only for high-conviction ideas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "US ticker, e.g. NVDA",
                    },
                    "dollar_amount": {
                        "type": "number",
                        "description": f"Budget in USD (default {_trade_amount:.0f})",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short thesis for the journal",
                    },
                    "score": {
                        "type": "number",
                        "description": "Risk-adjusted conviction 0-10",
                    },
                },
                "required": ["symbol"],
            },
        },
        {
            "type": "function",
            "name": "sell_stock",
            "description": (
                "Close an entire PAPER position for a symbol (exit / take profit / cut loss)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "US ticker to fully exit",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why selling",
                    },
                },
                "required": ["symbol"],
            },
        },
    ]


def trading_system_addon() -> str:
    return (
        "\n\nPAPER TRADING TOOLS (enabled):\n"
        "You may place PAPER trades on Alpaca using the tools buy_stock, sell_stock, "
        "list_positions, and get_account. This is simulated money only.\n"
        f"- Default size ≈ ${_trade_amount:.0f} per buy; max {_max_positions} open positions; "
        f"max {_max_buys_per_poll} new buys this research run.\n"
        "- Prefer high-conviction names (score ≥ 7). Check get_account + list_positions first.\n"
        "- After ranking finalists, BUY the best ideas that fit the budget/caps, "
        "and SELL any held names whose thesis is invalidated.\n"
        "- Never claim a trade succeeded unless a tool returned ok=true.\n"
        "- Still end with the JSON suggestions trailer for the desk panel."
    )
