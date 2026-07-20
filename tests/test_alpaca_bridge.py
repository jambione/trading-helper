"""Unit tests for the Alpaca bridge provider (no live network)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from webull_bridge.config import DEFAULTS
from webull_bridge.providers.alpaca import (
    quote_to_book,
    _map_order,
    _map_order_status,
    _paper,
)


# ── quote → depth-1 book ────────────────────────────────────────────────


def test_quote_to_book_from_object():
    q = SimpleNamespace(
        bid_price=100.0, ask_price=100.05,
        bid_size=200, ask_size=150,
    )
    book = quote_to_book(q, ts=1_700_000_000.0)
    assert book is not None
    assert book.best_bid == 100.0 and book.best_ask == 100.05
    assert book.bids == [(100.0, 200.0)]
    assert book.asks == [(100.05, 150.0)]
    assert book.ts == 1_700_000_000.0
    assert book.imbalance == pytest.approx(200 / 150)


def test_quote_to_book_from_dict():
    book = quote_to_book({
        "bid_price": "10.25", "ask_price": "10.30",
        "bid_size": "50", "ask_size": "40",
    })
    assert book is not None
    assert book.best_bid == 10.25
    assert len(book.bids) == 1 and len(book.asks) == 1


def test_quote_to_book_rejects_bad():
    assert quote_to_book(None) is None
    assert quote_to_book(SimpleNamespace(
        bid_price=0, ask_price=1, bid_size=1, ask_size=1)) is None
    # crossed
    assert quote_to_book(SimpleNamespace(
        bid_price=10.5, ask_price=10.0, bid_size=1, ask_size=1)) is None
    # locked (bid == ask)
    assert quote_to_book(SimpleNamespace(
        bid_price=10.0, ask_price=10.0, bid_size=1, ask_size=1)) is None


def test_quote_to_book_zero_size_ok():
    """Outside RTH sizes may be 0; mid/spread still useful."""
    book = quote_to_book(SimpleNamespace(
        bid_price=50.0, ask_price=50.1, bid_size=0, ask_size=0))
    assert book is not None
    assert book.mid == pytest.approx(50.05)


# ── order mapping ───────────────────────────────────────────────────────


def test_map_order_status():
    assert _map_order_status("filled") == "FILLED"
    assert _map_order_status("OrderStatus.FILLED") == "FILLED"
    assert _map_order_status("canceled") == "CANCELLED"
    assert _map_order_status("rejected") == "REJECTED"
    assert _map_order_status("new") == "PENDING"
    assert _map_order_status("partially_filled") == "PENDING"


def test_map_order():
    o = SimpleNamespace(
        id="abc-123",
        symbol="aapl",
        side="buy",
        type="market",
        qty="10",
        limit_price=None,
        status="filled",
        filled_qty="10",
        filled_avg_price="190.5",
        submitted_at=None,
        created_at=None,
    )
    m = _map_order(o)
    assert m.id == "abc-123"
    assert m.symbol == "AAPL"
    assert m.side == "BUY"
    assert m.order_type == "MARKET"
    assert m.qty == 10.0
    assert m.status == "FILLED"
    assert m.filled_qty == 10.0
    assert m.avg_price == 190.5


def test_paper_default():
    assert _paper({}) is True
    assert _paper({"alpaca_paper": False}) is False
    assert _paper({"alpaca_paper": True}) is True


# ── factory wiring ──────────────────────────────────────────────────────


def test_build_providers_alpaca_branch():
    """Factory selects Alpaca classes when name is alpaca (ctors mocked)."""
    import webull_bridge.providers as providers

    fake_md = object()
    fake_br = object()
    cfg = {**DEFAULTS, "provider": "alpaca",
           "alpaca_api_key": "PK_TEST", "alpaca_secret_key": "SECRET"}

    with patch("webull_bridge.providers.alpaca.AlpacaMarketData",
               return_value=fake_md) as md_cls, \
         patch("webull_bridge.providers.alpaca.AlpacaBroker",
               return_value=fake_br) as br_cls:
        md, br = providers.build_providers(cfg)
        assert md is fake_md
        assert br is fake_br
        md_cls.assert_called_once()
        br_cls.assert_called_once()


def test_build_providers_split_alpaca_broker_mock_md():
    """broker_provider=alpaca with mock market data still works."""
    import webull_bridge.providers as providers
    from webull_bridge.providers.mock import MockMarketData

    fake_br = object()
    cfg = {**DEFAULTS, "provider": "mock",
           "broker_provider": "alpaca",
           "market_data_provider": "mock",
           "alpaca_api_key": "PK_TEST", "alpaca_secret_key": "SECRET"}

    with patch("webull_bridge.providers.alpaca.AlpacaBroker",
               return_value=fake_br):
        md, br = providers.build_providers(cfg)
        assert isinstance(md, MockMarketData)
        assert br is fake_br


# ── broker methods against a fake TradingClient ─────────────────────────


def test_alpaca_broker_account_positions_orders():
    from webull_bridge.providers.alpaca import AlpacaBroker

    fake_client = MagicMock()
    fake_client.get_account.return_value = SimpleNamespace(
        id="acct-1", cash="100000", buying_power="200000",
        account_number="PA123",
    )
    fake_client.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty="3.5",
                        avg_entry_price="180", current_price="190"),
    ]
    fake_client.get_orders.return_value = [
        SimpleNamespace(
            id="ord-1", symbol="AAPL", side="buy", type="limit",
            qty="3.5", limit_price="179", status="new",
            filled_qty="0", filled_avg_price=None,
            submitted_at=None, created_at=None,
        ),
    ]

    with patch("webull_bridge.providers.alpaca._require_sdk"), \
         patch("webull_bridge.providers.alpaca._credentials",
               return_value=("K", "S")), \
         patch("alpaca.trading.client.TradingClient",
               return_value=fake_client):
        broker = AlpacaBroker({"alpaca_paper": True})

    async def run():
        acct = await broker.account()
        assert acct.account_id == "acct-1"
        assert acct.cash == 100000.0
        assert acct.paper is True

        pos = await broker.positions()
        assert len(pos) == 1
        assert pos[0].symbol == "AAPL" and pos[0].qty == 3.5

        orders = await broker.orders()
        assert any(o.id == "ord-1" and o.status == "PENDING" for o in orders)

    asyncio.run(run())


def test_alpaca_broker_place_and_cancel():
    from webull_bridge.providers.alpaca import AlpacaBroker

    fake_client = MagicMock()
    fake_client.submit_order.return_value = SimpleNamespace(
        id="new-1", symbol="MSFT", side="buy", type="market",
        qty="2", limit_price=None, status="accepted",
        filled_qty="0", filled_avg_price=None,
        submitted_at=None, created_at=None,
    )
    fake_client.cancel_order_by_id.return_value = None

    with patch("webull_bridge.providers.alpaca._require_sdk"), \
         patch("webull_bridge.providers.alpaca._credentials",
               return_value=("K", "S")), \
         patch("alpaca.trading.client.TradingClient",
               return_value=fake_client):
        broker = AlpacaBroker({"alpaca_paper": True})

    async def run():
        o = await broker.place_order("msft", "BUY", 2, "MARKET")
        assert o.id == "new-1"
        assert o.symbol == "MSFT"
        assert o.status == "PENDING"
        fake_client.submit_order.assert_called_once()

        assert await broker.cancel_order("new-1") is True
        fake_client.cancel_order_by_id.assert_called_with("new-1")

    asyncio.run(run())


def test_alpaca_market_data_fetch():
    from webull_bridge.providers.alpaca import AlpacaMarketData

    quote = SimpleNamespace(
        bid_price=10.0, ask_price=10.02, bid_size=100, ask_size=80,
    )
    fake_data = MagicMock()
    fake_data.get_stock_latest_quote.return_value = {"AAPL": quote}

    with patch("webull_bridge.providers.alpaca._require_sdk"), \
         patch("webull_bridge.providers.alpaca._credentials",
               return_value=("K", "S")), \
         patch("alpaca.data.historical.StockHistoricalDataClient",
               return_value=fake_data):
        md = AlpacaMarketData({"alpaca_poll_sec": 0.1})

    book = md._fetch("AAPL")
    assert book is not None
    assert book.best_bid == 10.0 and book.best_ask == 10.02

    async def snap():
        s = await md.snapshot("AAPL")
        assert s is not None
        assert s.best_bid == 10.0

    asyncio.run(snap())
