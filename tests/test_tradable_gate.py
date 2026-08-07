"""
test_tradable_gate.py — buys are refused for symbols Alpaca will not trade.

The momentum panel feeds buy candidates off an OCR read of a Discord scanner,
so two kinds of symbol reached order submission with nothing to stop them:
names Alpaca has never heard of ($BOM, $NIANI — "asset not found") and
delisted names that still quote ($HOM, INACTIVE, last print 2023-04-11). A
three-year-old price would have sized the order and set the stop; the existing
guard only checks stop < limit, not whether either number describes today.

Run:
    venv/bin/python -m pytest tests/test_tradable_gate.py -q
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca_trader as at  # noqa: E402

# Captured before conftest's _permissive_tradability can swap it out. That
# fixture exists so order-mechanics tests can place buys without an asset
# lookup; this file is the one place that must exercise the real thing.
_REAL_SYMBOL_TRADABLE = at.symbol_tradable


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    at.symbol_tradable = _REAL_SYMBOL_TRADABLE
    at._asset_ok.clear()
    monkeypatch.setattr(at, "_mode", "paper")
    monkeypatch.setattr(at, "_host_allowed", lambda: True)
    yield
    at._asset_ok.clear()


def _client_returning(**assets):
    c = MagicMock()

    def get_asset(sym):
        if sym not in assets:
            raise Exception('{"code":40410000,"message":"asset not found"}')
        return assets[sym]
    c.get_asset.side_effect = get_asset
    return c


ACTIVE = SimpleNamespace(tradable=True, status="AssetStatus.ACTIVE")
INACTIVE = SimpleNamespace(tradable=False, status="AssetStatus.INACTIVE")


def test_tradable_symbol_passes(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(GLXG=ACTIVE))
    assert at.symbol_tradable("GLXG") is True


def test_delisted_symbol_that_still_quotes_is_refused(monkeypatch):
    """$HOM is INACTIVE and last printed in 2023, yet returns a price."""
    monkeypatch.setattr(at, "_client", _client_returning(HOM=INACTIVE))
    assert at.symbol_tradable("HOM") is False


def test_unknown_symbol_is_refused(monkeypatch):
    """OCR inventions and OTC names Alpaca has never heard of."""
    monkeypatch.setattr(at, "_client", _client_returning(GLXG=ACTIVE))
    assert at.symbol_tradable("BOM") is False
    assert at.symbol_tradable("NIANI") is False


def test_transient_lookup_failure_fails_closed_and_is_not_cached(monkeypatch):
    """Refusing one buy costs a missed entry; allowing one because the lookup
    broke is the trade this exists to prevent. But a network blip must not
    blacklist a good symbol for the rest of the session."""
    c = MagicMock()
    c.get_asset.side_effect = Exception("503 service unavailable")
    monkeypatch.setattr(at, "_client", c)
    assert at.symbol_tradable("GLXG") is False
    assert "GLXG" not in at._asset_ok, "transient failure must not be cached"

    monkeypatch.setattr(at, "_client", _client_returning(GLXG=ACTIVE))
    assert at.symbol_tradable("GLXG") is True


def test_definitive_not_found_is_cached(monkeypatch):
    c = _client_returning(GLXG=ACTIVE)
    monkeypatch.setattr(at, "_client", c)
    assert at.symbol_tradable("BOM") is False
    assert at.symbol_tradable("BOM") is False
    assert c.get_asset.call_count == 1, "definitive answer should be cached"


def test_no_client_refuses(monkeypatch):
    monkeypatch.setattr(at, "_client", None)
    assert at.symbol_tradable("GLXG") is False


def test_blank_symbol_refuses(monkeypatch):
    monkeypatch.setattr(at, "_client", _client_returning(GLXG=ACTIVE))
    assert at.symbol_tradable("") is False
    assert at.symbol_tradable(None) is False


# ── The exit path must never be gated on this ────────────────────────────────

def test_sell_path_does_not_consult_tradability(monkeypatch):
    """A position in a symbol that has since gone untradable still has to be
    closable. Gating exits would trap the desk in exactly the names it should
    be getting out of."""
    import inspect
    for name in ("sell", "cancel_open_orders", "liquidate_all"):
        fn = getattr(at, name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        assert "symbol_tradable" not in src, (
            f"{name}() must not gate on tradability — exits have to work")


def test_every_buy_path_is_gated():
    """Each buy entry point consults the gate. buy_limit_at_ask delegates to
    buy_limit_at_price, so it is covered transitively."""
    import inspect
    for name in ("buy", "buy_limit_at_price", "buy_market_shares",
                 "buy_limit_bracket", "buy_bracket_exact"):
        src = inspect.getsource(getattr(at, name))
        assert "symbol_tradable" in src, f"{name}() is not gated"
