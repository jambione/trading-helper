"""Trailing stop helper in alpaca_trader (mocked client)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import alpaca_trader as tr


@pytest.fixture(autouse=True)
def _allow_this_host(monkeypatch):
    """Order mechanics must not depend on which box runs the suite.

    ai_trading_host names the Mac mini, so _host_allowed() is False anywhere
    else and every mutating path returns trader_off. The guard has its own
    coverage in test_trading_host_guard.py; here it is noise.
    """
    monkeypatch.setattr(tr, "_host_allowed", lambda: True)


def test_place_trailing_stop():
    fake = MagicMock()
    fake.get_open_position.return_value = SimpleNamespace(qty="12.5")
    fake.submit_order.return_value = SimpleNamespace(
        id="trail-1", status="accepted")
    tr._mode = "paper"
    tr._client = fake

    with patch.object(tr, "_log_action"):
        out = tr.place_trailing_stop("TSLA", trail_percent=15.0)

    assert out["ok"] is True
    assert out["order_id"] == "trail-1"
    req = fake.submit_order.call_args[0][0]
    assert float(req.trail_percent) == 15.0
    assert float(req.qty) == 12.5
