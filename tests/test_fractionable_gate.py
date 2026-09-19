"""symbol_fractionable fails closed; only definitive answers are cached."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca_trader as at  # noqa: E402

_REAL_SYMBOL_FRACTIONABLE = at.symbol_fractionable
_REAL_SYMBOL_TRADABLE = at.symbol_tradable


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    at.symbol_fractionable = _REAL_SYMBOL_FRACTIONABLE
    at.symbol_tradable = _REAL_SYMBOL_TRADABLE
    at._asset_ok.clear()
    at._asset_fractionable.clear()
    monkeypatch.setattr(at, "_mode", "paper")
    monkeypatch.setattr(at, "_host_allowed", lambda: True)
    yield
    at._asset_ok.clear()
    at._asset_fractionable.clear()


def _client_returning(**assets):
    c = MagicMock()

    def get_asset(sym):
        if sym not in assets:
            raise Exception('{"code":40410000,"message":"asset not found"}')
        return assets[sym]

    c.get_asset.side_effect = get_asset
    return c


FRAC = SimpleNamespace(
    tradable=True, status="AssetStatus.ACTIVE", fractionable=True)
WHOLE = SimpleNamespace(
    tradable=True, status="AssetStatus.ACTIVE", fractionable=False)


def test_fractionable_true(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(AAPL=FRAC))
    assert at.symbol_fractionable("AAPL") is True


def test_not_fractionable(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(DFDV=WHOLE))
    assert at.symbol_fractionable("DFDV") is False


def test_lookup_failure_fails_closed_not_cached(monkeypatch):
    c = MagicMock()
    c.get_asset.side_effect = Exception("503 service unavailable")
    monkeypatch.setattr(at, "_client", c)
    assert at.symbol_fractionable("AAPL") is False
    assert "AAPL" not in at._asset_fractionable


def test_not_found_cached_as_false(monkeypatch):
    c = _client_returning(AAPL=FRAC)
    monkeypatch.setattr(at, "_client", c)
    assert at.symbol_fractionable("BOM") is False
    assert at.symbol_fractionable("BOM") is False
    assert c.get_asset.call_count == 1


def test_no_client_fails_closed(monkeypatch):
    monkeypatch.setattr(at, "_client", None)
    assert at.symbol_fractionable("AAPL") is False


def test_tradable_fetch_also_caches_fractionable(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(AAPL=FRAC))
    assert at.symbol_tradable("AAPL") is True
    assert at._asset_fractionable["AAPL"] is True
    # Second call should not re-fetch.
    c = at._client
    assert at.symbol_fractionable("AAPL") is True
    assert c.get_asset.call_count == 1


def test_want_fractional_respects_ext_hours_and_flag(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(AAPL=FRAC))
    monkeypatch.setattr(at, "_fractional_shares_enabled", lambda: True)
    assert at.want_fractional("AAPL", extended_hours=False) is True
    assert at.want_fractional("AAPL", extended_hours=True) is False
    monkeypatch.setattr(at, "_fractional_shares_enabled", lambda: False)
    assert at.want_fractional("AAPL", extended_hours=False) is False


def test_order_qty_fractional_and_whole():
    assert at._order_qty(2.934, fractional=False) == 2
    assert at._order_qty(2.934, fractional=True) == pytest.approx(2.934)
    assert at._order_qty(0.5, fractional=False) is None
    # Sub-$1 notional rejected when price known.
    assert at._order_qty(0.5, fractional=True, price=1.0) is None
    assert at._order_qty(1.2, fractional=True, price=1.0) == pytest.approx(1.2)
