"""Unit tests for alpaca_price_poll (no network)."""
import time

import alpaca_price_poll as px


def test_update_and_get_price():
    px.STATE.prices.clear()
    px.STATE.update("aapl", 190.5, 10)
    assert px.get_latest_price("AAPL") == 190.5
    assert px.get_latest_price("MSFT") is None


def test_subscribe_set():
    px.STATE.subscribed.clear()
    px.request_subscribe(["nvda", "amd"])
    assert "NVDA" in px.STATE.subscribed
    assert "AMD" in px.STATE.subscribed
    px.request_unsubscribe(["nvda"])
    assert "NVDA" not in px.STATE.subscribed


def test_stale_price_returns_none():
    px.STATE.prices.clear()
    px.STATE.update("X", 1.0, 1)
    with px.STATE.lock:
        px.STATE.prices["X"]["ts_unix"] = time.time() - 60
    assert px.get_latest_price("X") is None


# ── the trade's own clock, not the poll's ───────────────────────────────────
#
# 2026-08-27. The poll stamped every trade in the batch with `now`, its own
# read time. That clock feeds the realtime bar aggregator, which is where
# rt_price_age_sec comes from, which is what the dashboard's price merge uses
# to pick a winner. A twenty-minute-old print read as brand new and beat
# genuinely fresh quotes. The tell was eight symbols publishing an identical
# 0.4s age — real trade clocks do not line up.

class _FakeTrade:
    def __init__(self, price, size, timestamp):
        self.price = price
        self.size = size
        self.timestamp = timestamp


def _poll_once(monkeypatch, trades):
    """Drive one _poll_once with a stubbed client, capturing callback args."""
    import datetime as _dt

    import alpaca_price_poll as app

    seen = []
    monkeypatch.setattr(app, "_callbacks", [lambda *a: seen.append(a)])
    monkeypatch.setattr(app.STATE, "subscribed", set(trades), raising=False)

    class _C:
        def get_stock_latest_trade(self, _req):
            return trades

    monkeypatch.setattr(app, "_client", _C())
    app._poll_once()
    return seen, _dt


def test_the_callback_carries_the_trades_timestamp(monkeypatch):
    import datetime as dt

    when = dt.datetime(2026, 8, 27, 13, 31, 0, tzinfo=dt.timezone.utc)
    seen, _ = _poll_once(monkeypatch, {"AAA": _FakeTrade(10.0, 100, when)})
    assert seen, "callback never fired"
    _sym, _px, _sz, ts = seen[0]
    assert ts == when.timestamp()


def test_two_symbols_with_different_prints_get_different_clocks(monkeypatch):
    """The identical-age symptom, asserted directly."""
    import datetime as dt

    old = dt.datetime(2026, 8, 27, 13, 10, 0, tzinfo=dt.timezone.utc)
    new = dt.datetime(2026, 8, 27, 13, 31, 0, tzinfo=dt.timezone.utc)
    seen, _ = _poll_once(monkeypatch, {
        "OLD": _FakeTrade(10.0, 100, old),
        "NEW": _FakeTrade(20.0, 100, new),
    })
    stamps = {s: ts for s, _px, _sz, ts in seen}
    assert stamps["OLD"] != stamps["NEW"]
    assert stamps["NEW"] - stamps["OLD"] == 21 * 60


def test_a_trade_with_no_timestamp_falls_back_without_raising(monkeypatch):
    seen, _ = _poll_once(monkeypatch, {"AAA": _FakeTrade(10.0, 100, None)})
    assert seen
    assert isinstance(seen[0][3], float)
