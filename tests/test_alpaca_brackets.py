"""Bracket path in alpaca_trader (mocked TradingClient)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import alpaca_trader as tr


def test_buy_submits_bracket_when_enabled():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(
        id="br-1", status="accepted")
    tr._mode = "paper"
    tr._client = fake
    tr._trade_amount = 500.0
    tr._extended_hours = False
    tr._use_brackets = True
    tr._stop_loss_pct = 1.5
    tr._take_profit_pct = 3.0
    tr._limit_offset = 0.0

    with patch.object(tr, "_log_action"):
        out = tr.buy("AAPL", price=100.0, rsi=30.0, hist=0.1)

    assert out["ok"] is True
    assert out["order_id"] == "br-1"
    fake.submit_order.assert_called_once()
    req = fake.submit_order.call_args[0][0]
    # MarketOrderRequest with bracket class
    assert getattr(req, "order_class", None) is not None
    assert str(req.order_class).lower().endswith("bracket")
    assert float(req.qty) == 5.0  # 500/100
    assert req.take_profit is not None
    assert req.stop_loss is not None


def test_buy_notional_without_brackets():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(
        id="n-1", status="accepted")
    tr._mode = "paper"
    tr._client = fake
    tr._trade_amount = 250.0
    tr._extended_hours = False
    tr._use_brackets = False
    tr._stop_loss_pct = 0.0
    tr._take_profit_pct = 0.0

    with patch.object(tr, "_log_action"):
        out = tr.buy("MSFT", price=50.0, rsi=40.0, hist=0.0)

    assert out["ok"]
    req = fake.submit_order.call_args[0][0]
    assert float(req.notional) == 250.0
