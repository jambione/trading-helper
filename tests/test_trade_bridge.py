"""Tests for the trade_bridge package: engine stance transitions from
scripted book sequences, the order-value safety cap, and mock broker fills.
"""
import asyncio

import pytest

from trade_bridge.config import DEFAULTS
from trade_bridge.engine import SymbolEngine
from trade_bridge.l2 import L2Book
from trade_bridge.providers.mock import MockBroker, MockMarketData


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

# The trend pillar refuses to report a "5-minute trend" until
# trend_min_coverage (0.6) of trend_window (300s) is really covered -- 40s of
# history is never a 5m drift. These scripted runs feed only books, so trend
# is the ONLY pillar with an opinion (no tape, no vwap): a run shorter than
# 180s leaves zero live pillars and the stance is a permanent NEUTRAL.
# The original 120s runs predate that gate and tested a trend that would
# speak from any scrap of history. Cover the whole window instead.
READS_PER_SEC = 3
SPAN_SECS = 300.0
READS = int(READS_PER_SEC * SPAN_SECS)


def test_engine_reaches_long_stance(cfg):
    """Sustained buy imbalance + rising price -> LongView flips to LONG."""
    eng = SymbolEngine("TEST", cfg)
    t0 = 1_000_000.0
    state = None
    # a steady uptrend (+0.5% overall, comfortably above LongView's +0.1%
    # five-minute drift gate) across the full trend window
    for i in range(READS):
        ts = t0 + i / READS_PER_SEC
        mid = 5.0 * (1 + 0.005 * (i / READS))
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
    for i in range(READS):
        ts = t0 + i / READS_PER_SEC
        mid = 5.0 * (1 - 0.004 * (i / READS))    # −0.4% over the window
        state = eng.on_book(make_book(mid, 0.4, ts), now=ts)
    assert state["stance"]["stance"] == "BEAR"
    for key in ("symbol", "ts", "stance", "bias", "playbook", "book",
                "walls", "trend1", "trend5", "projection", "mids", "signal"):
        assert key in state, f"missing {key}"
    assert state["playbook"]["verdict"] in ("BEARISH", "BEAR_CRACK", "STAND_ASIDE")
    # Multi-level depth no longer claimed — mock may still send 10 levels,
    # but the API never reports walls.
    assert state["walls"] == []
    assert "touch_skew" in state["book"]


def test_engine_stance_has_hysteresis(cfg):
    """A brief flip in the inputs must NOT flip the stance immediately."""
    eng = SymbolEngine("TEST", cfg)
    t0 = 3_000_000.0
    ts = t0
    for i in range(READS):
        ts = t0 + i / READS_PER_SEC
        state = eng.on_book(make_book(5.0 + 0.01 * (i / 100), 2.0, ts), now=ts)
    assert state["stance"]["stance"] == "LONG"
    # 5 seconds of bearish books — shorter than long_confirm_secs (20)
    for i in range(15):
        ts += 1 / READS_PER_SEC
        state = eng.on_book(make_book(5.0, 0.4, ts), now=ts)
    assert state["stance"]["stance"] == "LONG"      # held by hysteresis
    assert state["stance"]["pending"] in (None, "BEAR", "NEUTRAL")


def test_walls_always_empty_at_depth1(cfg):
    """Phase 2b: multi-level walls are not claimed even if book has deep levels."""
    eng = SymbolEngine("TEST", cfg)
    book = make_book(5.0, 1.0, 4_000_000.0)
    asks = list(book.asks)
    asks[2] = (asks[2][0], asks[2][1] * 6)
    state = eng.on_book(L2Book(book.bids, asks, ts=book.ts))
    assert state["walls"] == []
    assert state["playbook"]["ask_break"] == []
    assert state["playbook"]["bid_crack"] == []


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


# ── auto-watch sync ──────────────────────────────────────────────────────


def test_auto_watch_sync(cfg):
    """sync_symbols keeps engines matched to the ticker list, respects the
    engine cap, and never drops a symbol a client is streaming."""
    from trade_bridge.engine import BridgeManager

    async def run():
        m = BridgeManager({**cfg, "max_engines": 3})
        m.sync_symbols(["SKYQ", "TSLA", "AMD", "NVDA"])   # NVDA over cap
        assert m.watching() == ["AMD", "SKYQ", "TSLA"]

        # a client streams TSLA; the ticker list moves on without it
        q = m.subscribe("TSLA")
        m.sync_symbols(["SKYQ", "NVDA"])
        assert "TSLA" in m.watching()          # protected by the subscriber
        assert "AMD" not in m.watching()       # dropped
        assert {"SKYQ", "NVDA"} <= set(m.watching())

        m.unsubscribe("TSLA", q)
        m.sync_symbols(["SKYQ", "NVDA"])
        assert "TSLA" not in m.watching()      # released once unsubscribed
        await m.shutdown()

    asyncio.run(run())



# ── order-value cap (route logic) ────────────────────────────────────────


def test_order_cap_rejection(cfg):
    """POST /api/broker/orders must reject orders above max_order_value."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import trade_bridge.routes as routes

    app = FastAPI()
    app.include_router(routes.router)
    routes._manager = None                # fresh manager with test cfg
    import trade_bridge.config as bcfg
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
