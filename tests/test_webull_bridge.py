"""Tests for the webull_bridge package: engine stance transitions from
scripted book sequences, the order-value safety cap, and mock broker fills.
"""
import asyncio

import pytest

from webull_bridge.config import DEFAULTS
from webull_bridge.engine import SymbolEngine
from webull_bridge.l2 import L2Book
from webull_bridge.providers.mock import MockBroker, MockMarketData


def make_book(mid: float, imbalance: float, ts: float,
              levels: int = 10, base: float = 500.0) -> L2Book:
    """Book with total bid/ask size ratio == imbalance, 1c spread/ticks."""
    bid_sz = base * (imbalance ** 0.5)
    ask_sz = base / (imbalance ** 0.5)
    best_bid = round(mid - 0.005, 3)
    best_ask = round(mid + 0.005, 3)
    bids = [(round(best_bid - i * 0.01, 3), bid_sz) for i in range(levels)]
    asks = [(round(best_ask + i * 0.01, 3), ask_sz) for i in range(levels)]
    return L2Book(bids, asks, ts=ts)


@pytest.fixture
def cfg():
    return dict(DEFAULTS)


# ── engine ───────────────────────────────────────────────────────────────


def test_engine_reaches_long_stance(cfg):
    """Sustained buy imbalance + rising price -> LongView flips to LONG."""
    eng = SymbolEngine("TEST", cfg)
    t0 = 1_000_000.0
    state = None
    # 120s of strong bid imbalance with a steady uptrend (+0.5% overall,
    # comfortably above LongView's +0.1% five-minute drift gate)
    for i in range(360):                       # 3 reads/sec
        ts = t0 + i / 3.0
        mid = 5.0 * (1 + 0.005 * (i / 360))
        state = eng.on_book(make_book(mid, 2.0, ts), now=ts)
    assert state["stance"]["stance"] == "LONG"
    assert state["bias"]["label"] == "BULLISH"
    assert state["book"]["imbalance"] == pytest.approx(2.0, abs=0.1)


def test_engine_bear_stance_and_shape(cfg):
    """Sell-side imbalance + falling price -> BEAR, and the state JSON has
    every field the iPhone app renders."""
    eng = SymbolEngine("TEST", cfg)
    t0 = 2_000_000.0
    state = None
    for i in range(360):
        ts = t0 + i / 3.0
        mid = 5.0 * (1 - 0.004 * (i / 360))    # −0.4% over 2 min
        state = eng.on_book(make_book(mid, 0.4, ts), now=ts)
    assert state["stance"]["stance"] == "BEAR"
    for key in ("symbol", "ts", "stance", "bias", "playbook", "book",
                "walls", "trend1", "trend5", "projection", "mids", "signal"):
        assert key in state, f"missing {key}"
    assert state["playbook"]["verdict"] in ("BEARISH", "BEAR_CRACK")
    assert len(state["book"]["bids"]) == 10


def test_engine_stance_has_hysteresis(cfg):
    """A brief flip in the inputs must NOT flip the stance immediately."""
    eng = SymbolEngine("TEST", cfg)
    t0 = 3_000_000.0
    ts = t0
    for i in range(360):
        ts = t0 + i / 3.0
        state = eng.on_book(make_book(5.0 + 0.01 * (i / 100), 2.0, ts), now=ts)
    assert state["stance"]["stance"] == "LONG"
    # 5 seconds of bearish books — shorter than long_confirm_secs (20)
    for i in range(15):
        ts += 1 / 3.0
        state = eng.on_book(make_book(5.0, 0.4, ts), now=ts)
    assert state["stance"]["stance"] == "LONG"      # held by hysteresis
    assert state["stance"]["pending"] in (None, "BEAR", "NEUTRAL")


def test_wall_detection(cfg):
    eng = SymbolEngine("TEST", cfg)
    book = make_book(5.0, 1.0, 4_000_000.0)
    # plant a 6x wall on the 3rd ask level
    asks = list(book.asks)
    asks[2] = (asks[2][0], asks[2][1] * 6)
    state = eng.on_book(L2Book(book.bids, asks, ts=book.ts))
    assert ["ASK", asks[2][0], int(asks[2][1])] in state["walls"]


# ── mock providers ───────────────────────────────────────────────────────


def test_mock_market_data_books_are_valid(cfg):
    md = MockMarketData(cfg)
    sim = md._sim("SKYQ")
    for i in range(200):
        book = sim.step(5_000_000.0 + i / 3.0)
        assert book.best_bid < book.best_ask
        assert all(book.bids[i][0] >= book.bids[i + 1][0]
                   for i in range(len(book.bids) - 1))
        assert all(book.asks[i][0] <= book.asks[i + 1][0]
                   for i in range(len(book.asks) - 1))
        assert len(book.bids) == 10 and len(book.asks) == 10


def test_mock_broker_market_fill_and_position(cfg):
    async def run():
        md = MockMarketData(cfg)
        broker = MockBroker(cfg, md)
        start_cash = (await broker.account()).cash
        order = await broker.place_order("SKYQ", "BUY", 10, "MARKET")
        assert order.status == "FILLED"
        assert order.avg_price is not None and order.avg_price > 0
        pos = await broker.positions()
        assert len(pos) == 1 and pos[0].qty == 10
        assert (await broker.account()).cash == pytest.approx(
            start_cash - 10 * order.avg_price)
        # sell it all back -> flat
        await broker.place_order("SKYQ", "SELL", 10, "MARKET")
        assert await broker.positions() == []
    asyncio.run(run())


def test_mock_broker_limit_and_cancel(cfg):
    async def run():
        md = MockMarketData(cfg)
        broker = MockBroker(cfg, md)
        book = await md.snapshot("SKYQ")
        # a buy limit far below the market must rest, then cancel cleanly
        order = await broker.place_order("SKYQ", "BUY", 5, "LIMIT",
                                         limit_price=round(book.best_bid * 0.5, 2))
        assert order.status == "PENDING"
        assert await broker.cancel_order(order.id) is True
        assert (await broker.orders())[0].status == "CANCELLED"
        assert await broker.cancel_order(order.id) is False
    asyncio.run(run())


# ── webull depth payload parser ──────────────────────────────────────────


def test_parse_depth_payload():
    from webull_bridge.providers.webull import parse_depth_payload

    payload = {
        "symbol": "AAPL",
        "asks": [{"price": "231.45", "size": "300"},
                 {"price": "231.46", "size": "1,200"}],
        "bids": [{"price": "231.44", "size": "500"},
                 {"price": "231.43", "size": "800"}],
    }
    book = parse_depth_payload(payload)
    assert book is not None
    assert book.best_bid == 231.44 and book.best_ask == 231.45
    assert book.bids[1] == (231.43, 800.0)
    assert book.asks[1] == (231.46, 1200.0)

    # askList/bidList spelling also parses
    alt = {"askList": [{"price": 5.01, "size": 10}],
           "bidList": [{"price": 5.00, "size": 20}]}
    assert parse_depth_payload(alt) is not None

    # crossed or empty books are rejected
    crossed = {"asks": [{"price": "4.99", "size": "10"}],
               "bids": [{"price": "5.00", "size": "10"}]}
    assert parse_depth_payload(crossed) is None
    assert parse_depth_payload({}) is None


# ── order-value cap (route logic) ────────────────────────────────────────


def test_order_cap_rejection(cfg):
    """POST /api/broker/orders must reject orders above max_order_value."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webull_bridge.routes as routes

    app = FastAPI()
    app.include_router(routes.router)
    routes._manager = None                # fresh manager with test cfg
    import webull_bridge.config as bcfg
    orig = bcfg.load_config
    bcfg.load_config = lambda: {**DEFAULTS, "max_order_value": 50.0}
    routes.load_config = bcfg.load_config
    try:
        with TestClient(app) as client:
            r = client.post("/api/broker/orders",
                            json={"symbol": "SKYQ", "side": "BUY",
                                  "qty": 100000, "order_type": "MARKET"})
            assert r.status_code == 400
            assert "max_order_value" in r.json()["error"]

            r = client.post("/api/broker/orders",
                            json={"symbol": "SKYQ", "side": "BUY", "qty": 1,
                                  "order_type": "LIMIT", "limit_price": 5.0})
            assert r.status_code == 200
            assert r.json()["ok"] is True
    finally:
        bcfg.load_config = orig
        routes.load_config = orig
        routes._manager = None
