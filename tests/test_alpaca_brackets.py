"""Bracket path in alpaca_trader (mocked TradingClient)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import alpaca_trader as tr


@pytest.fixture(autouse=True)
def _restore_protection_guard():
    """One test rebinds the module-level no-naked-buy guard; without this it
    leaks into every later test file and silently disables the policy."""
    orig = tr._require_protective_exit
    yield
    tr._require_protective_exit = orig


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
    # Asserts the NOTIONAL order shape when brackets are off. The broker layer
    # now refuses that path by default (no protective exit, no position — see
    # test_protective_exit_required.py, which covers the refusal). Disable the
    # policy so the order-construction coverage survives.
    tr._require_protective_exit = lambda: False

    with patch.object(tr, "_log_action"):
        out = tr.buy("MSFT", price=50.0, rsi=40.0, hist=0.0)

    assert out["ok"]
    req = fake.submit_order.call_args[0][0]
    assert float(req.notional) == 250.0


def test_bracket_stop_is_stop_market_by_default(monkeypatch):
    """A stop-LIMIT with ~0.1% of room can miss entirely on a gap, leaving the
    position naked long with no working stop — on exactly the high-RVOL names
    the watch book selects for."""
    import alpaca_trader as at

    captured = {}

    class _Order:
        id = "o1"
        status = "accepted"
        legs = []

    class _Client:
        def submit_order(self, req):
            captured["req"] = req
            return _Order()

    monkeypatch.setattr(at, "_client", _Client())
    monkeypatch.setattr(at, "is_active", lambda: True)
    monkeypatch.setattr(at, "_stop_use_market", lambda: True)
    monkeypatch.setattr(at, "_log_action", lambda *a, **k: None)

    out = at.buy_bracket_exact("NVDA", 10, stop_price=38.0, target_price=46.0)
    assert out["ok"] is True
    sl = captured["req"].stop_loss
    assert float(sl.stop_price) == 38.0
    assert getattr(sl, "limit_price", None) is None, "must be a stop-MARKET"


def test_bracket_stop_limit_form_uses_configured_slippage(monkeypatch):
    import alpaca_trader as at

    captured = {}

    class _Order:
        id = "o1"
        status = "accepted"
        legs = []

    class _Client:
        def submit_order(self, req):
            captured["req"] = req
            return _Order()

    monkeypatch.setattr(at, "_client", _Client())
    monkeypatch.setattr(at, "is_active", lambda: True)
    monkeypatch.setattr(at, "_stop_use_market", lambda: False)
    monkeypatch.setattr(at, "_stop_limit_slip_pct", lambda: 1.0)
    monkeypatch.setattr(at, "_log_action", lambda *a, **k: None)

    at.buy_bracket_exact("NVDA", 10, stop_price=38.0, target_price=46.0)
    sl = captured["req"].stop_loss
    assert float(sl.limit_price) == 37.62      # 38.00 * (1 - 1%)
