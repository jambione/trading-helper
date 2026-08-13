"""HTTP + WebSocket API for the Mobile Trader app.

HTTP endpoints are protected by dashboard.py's auth middleware (Bearer
token). The WebSocket authenticates itself via ?token=, same as /ws.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from auth import is_auth_required, verify_token
from trade_bridge.config import load_config
from trade_bridge.engine import BridgeManager

log = logging.getLogger(__name__)
router = APIRouter()

_manager: BridgeManager | None = None


def get_manager() -> BridgeManager:
    global _manager
    if _manager is None:
        _manager = BridgeManager(load_config())
    return _manager


# ── L2 monitor ───────────────────────────────────────────────────────────


@router.get("/api/l2/watchlist")
async def l2_watchlist():
    m = get_manager()
    return JSONResponse({
        "provider": m.cfg.get("provider", "mock"),
        "symbols": [{
            "symbol": s,
            "stance": (m.state(s) or {}).get("stance", {}).get("stance"),
            "bias": (m.state(s) or {}).get("bias", {}).get("score"),
        } for s in m.watching()],
    })


class WatchBody(BaseModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalpha() or not 1 <= len(v) <= 5:
            raise ValueError("symbol must be 1-5 letters")
        return v


@router.post("/api/l2/watch")
async def l2_watch(body: WatchBody):
    m = get_manager()
    if not m.watch(body.symbol):
        return JSONResponse(
            {"ok": False, "error": f"engine limit reached "
             f"({m.cfg.get('max_engines', 8)}); unwatch something first"},
            status_code=409)
    return JSONResponse({"ok": True, "symbol": body.symbol})


@router.delete("/api/l2/watch/{symbol}")
async def l2_unwatch(symbol: str):
    get_manager().unwatch(symbol)
    return JSONResponse({"ok": True})


@router.get("/api/l2/{symbol}")
async def l2_state(symbol: str):
    m = get_manager()
    state = m.state(symbol)
    if state is None:
        if symbol.upper() not in m.watching():
            return JSONResponse({"ok": False, "error": "not watched"},
                                status_code=404)
        return JSONResponse({"ok": True, "symbol": symbol.upper(),
                             "warming_up": True})
    return JSONResponse(state)


@router.websocket("/ws/l2/{symbol}")
async def l2_stream(ws: WebSocket, symbol: str, token: str = ""):
    await ws.accept()
    if is_auth_required() and not verify_token(token):
        await ws.close(code=4001)
        return
    m = get_manager()
    symbol = symbol.upper()
    if not m.watch(symbol):
        await ws.close(code=4002, reason="engine limit reached")
        return
    q = m.subscribe(symbol)
    try:
        last = m.state(symbol)
        if last:
            await ws.send_json(last)
        while True:
            state = await q.get()
            if state is None:      # symbol was unwatched
                break
            await ws.send_json(state)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        m.unsubscribe(symbol, q)


# ── broker ───────────────────────────────────────────────────────────────


@router.get("/api/broker/account")
async def broker_account():
    acct = await get_manager().broker.account()
    return JSONResponse(acct.to_dict())


@router.get("/api/broker/positions")
async def broker_positions():
    pos = await get_manager().broker.positions()
    return JSONResponse({"positions": [p.to_dict() for p in pos]})


@router.get("/api/broker/orders")
async def broker_orders():
    orders = await get_manager().broker.orders()
    return JSONResponse({"orders": [o.to_dict() for o in orders]})


class OrderBody(BaseModel):
    symbol: str
    side: str                    # BUY | SELL
    qty: float
    order_type: str = "MARKET"   # MARKET | LIMIT
    limit_price: float | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalpha() or not 1 <= len(v) <= 5:
            raise ValueError("symbol must be 1-5 letters")
        return v

    @field_validator("side")
    @classmethod
    def _side(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return v

    @field_validator("qty")
    @classmethod
    def _qty(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("qty must be positive")
        return v

    @field_validator("order_type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("MARKET", "LIMIT"):
            raise ValueError("order_type must be MARKET or LIMIT")
        return v


@router.post("/api/broker/orders")
async def broker_place_order(body: OrderBody):
    m = get_manager()
    if body.side == "BUY":
        # Bare Market/Limit buys here never attach a stop and never call
        # alpaca_trader — the CELH failure mode in a second module. Exits
        # (SELL) still go through; entries belong on the AI desk brackets.
        return JSONResponse(
            {"ok": False,
             "error": "require_protective_exit: bridge buys are disabled; "
                      "use the AI desk bracket path (alpaca_trader)"},
            status_code=403)
    if body.order_type == "LIMIT" and not body.limit_price:
        return JSONResponse({"ok": False,
                             "error": "limit_price required for LIMIT orders"},
                            status_code=400)

    # estimate notional value for the safety cap: limit price if given,
    # otherwise the current touch from the depth feed
    price = body.limit_price
    if price is None:
        book = await m.market_data.snapshot(body.symbol)
        if book is None:
            return JSONResponse({"ok": False,
                                 "error": "no market data for symbol"},
                                status_code=400)
        price = book.best_ask if body.side == "BUY" else book.best_bid

    cap = float(m.cfg.get("max_order_value", 1000.0))
    est_value = price * body.qty
    if est_value > cap:
        return JSONResponse(
            {"ok": False,
             "error": f"order value ${est_value:,.2f} exceeds the "
                      f"max_order_value cap ${cap:,.2f}"},
            status_code=400)

    order = await m.broker.place_order(body.symbol, body.side, body.qty,
                                       body.order_type, body.limit_price)
    log.info("[BROKER] %s %s %.4g %s @ %s -> %s", order.id, body.side,
             body.qty, body.symbol, body.limit_price or "MKT", order.status)
    return JSONResponse({"ok": True, "order": order.to_dict()})


@router.delete("/api/broker/orders/{order_id}")
async def broker_cancel(order_id: str):
    ok = await get_manager().broker.cancel_order(order_id)
    if not ok:
        return JSONResponse({"ok": False,
                             "error": "order not found or not cancellable"},
                            status_code=404)
    return JSONResponse({"ok": True})
