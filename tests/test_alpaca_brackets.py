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


# ── the flag describes the session, not the desk ─────────────────────────────

def _clock(tr, *, is_open):
    """Set the session by patching the clock, not the cache wrapper.

    Replacing _market_open_cached leaks into every later test in the module —
    it is a module global with no fixture restoring it.
    """
    tr._clock_cache = (0.0, False)
    tr.market_is_open = lambda: bool(is_open)


def test_ext_hours_flag_is_off_inside_rth_so_brackets_are_accepted():
    """A BRACKET carrying extended_hours is refused by Alpaca outright.

    `_extended_hours` is desk policy — "this desk may trade outside RTH" — and
    was submitted verbatim on every order for the whole session. So during
    regular hours, when a bracket is both legal and required, every protected
    entry came back "bracket orders do not support extended hours trading":
    192 of them on 2026-08-07, which was 100% of the desk's attempts. The
    unprotected fallback is refused by _require_protective_exit, so the desk
    could not buy at all, at any hour.
    """
    import alpaca_trader as tr

    tr._extended_hours = True           # desk MAY trade outside RTH
    _clock(tr, is_open=True)            # ...but right now it is regular hours
    assert tr.ext_hours_now() is False

    _clock(tr, is_open=False)
    assert tr.ext_hours_now() is True

    # Policy off means off in every session.
    tr._extended_hours = False
    _clock(tr, is_open=False)
    assert tr.ext_hours_now() is False


def test_the_session_is_cached_so_it_is_not_asked_per_order_leg():
    """Consulted per leg, against a rate limit four processes share."""
    import alpaca_trader as tr

    calls = []
    orig = tr.market_is_open
    tr.market_is_open = lambda: (calls.append(1), True)[1]
    tr._clock_cache = (0.0, False)
    try:
        assert tr._market_open_cached() is True
        assert tr._market_open_cached() is True
        assert tr._market_open_cached() is True
        assert len(calls) == 1, "session re-read once per leg"
    finally:
        tr.market_is_open = orig
        tr._clock_cache = (0.0, False)


def test_dedupe_keeps_both_bracket_legs():
    """A bracket rests TWO sell orders for one symbol; neither is a duplicate.

    dedupe_open_orders grouped on (symbol, side) alone, so the take-profit
    (limit) and the stop-loss (stop) collided and one was cancelled. It runs
    from cleanup_duplicate_orders() on every ai_trader start, so restarting
    with positions open stripped protection from all of them — which is what a
    position_unprotected event and an empty order book showed on 2026-08-07.
    """
    import alpaca_trader as tr
    from types import SimpleNamespace
    from datetime import datetime, timezone

    def _o(oid, side, otype, mins):
        return SimpleNamespace(
            id=oid, symbol="NVDA", side=side, type=otype,
            submitted_at=datetime(2026, 8, 7, 14, mins, tzinfo=timezone.utc))

    orders = [
        _o("tp", "sell", "limit", 20),     # take-profit leg
        _o("sl", "sell", "stop", 20),      # stop-loss leg
        _o("buy_old", "buy", "limit", 10),  # genuinely stacked buys
        _o("buy_new", "buy", "limit", 30),
    ]

    canceled = []

    class _C:
        def get_orders(self, filter=None):
            return orders

        def cancel_order_by_id(self, oid):
            canceled.append(oid)

    orig_mode, orig_client = tr._mode, tr._client
    tr._mode, tr._client = "paper", _C()
    try:
        out = tr.dedupe_open_orders(keep="newest")
    finally:
        tr._mode, tr._client = orig_mode, orig_client

    assert out["ok"] is True
    assert "tp" not in canceled, "take-profit leg must survive"
    assert "sl" not in canceled, "stop-loss leg must survive"
    assert canceled == ["buy_old"], "only the stacked buy is a duplicate"
