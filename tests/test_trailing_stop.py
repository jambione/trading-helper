"""Trailing stop helper in alpaca_trader (mocked client)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import alpaca_trader as tr


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
