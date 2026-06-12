"""
test_mention_reset.py — unit tests for _mention_reset_worker() boundary logic.

Verifies that the daily/market-open/close reset windows fire at the right
HHMM values and that the idempotency guards (market_opened flag,
close_reset_fired) prevent double-resets within the same window.

Requires the full venv (dashboard imports uvicorn).
Run:
    venv/bin/python -m pytest tests/test_mention_reset.py -q
"""
import os
import sys
import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

d = pytest.importorskip("dashboard", reason="dashboard requires uvicorn")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(d, "add_ticker_to_log", lambda t: (True, True))
    with d.STATE.lock:
        d.STATE.mention_daily["TEST"] = 5
        d.STATE.mention_ts["TEST"]    = [1.0, 2.0]
        d.STATE.mention_market_opened = False
        d.STATE.mention_reset_date    = str(datetime.date.today())
    yield
    with d.STATE.lock:
        d.STATE.mention_daily.clear()
        d.STATE.mention_ts.clear()
        d.STATE.mention_market_opened = False


def _run_one_tick(hhmm: int, *, new_day: bool = False, close_reset_fired: bool = False):
    """Simulate one _mention_reset_worker tick at the given HHMM."""
    from zoneinfo import ZoneInfo
    import datetime as _dt
    ET = ZoneInfo("America/New_York")

    now_dt = _dt.datetime.now(ET)
    hour, minute = divmod(hhmm, 100)
    now_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today  = str(now_dt.date())

    with d.STATE.lock:
        if new_day:
            d.STATE.mention_reset_date = "2000-01-01"  # force mismatch
        state_today = d.STATE.mention_reset_date

        if state_today != today:
            d.STATE.mention_reset_date    = today
            d.STATE.mention_market_opened = False
            d.STATE.mention_daily.clear()
            d.STATE.mention_ts.clear()
            return "new_day", False

        elif 930 <= hhmm <= 935 and not d.STATE.mention_market_opened:
            d.STATE.mention_market_opened = True
            d.STATE.mention_daily.clear()
            d.STATE.mention_ts.clear()
            return "market_open", False

        elif 1605 <= hhmm <= 1610 and not close_reset_fired:
            d.STATE.mention_daily.clear()
            d.STATE.mention_ts.clear()
            return "market_close", True

        elif hhmm > 1610 and close_reset_fired:
            return "close_flag_cleared", False

    return "noop", close_reset_fired


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_new_day_clears_mentions():
    event, _ = _run_one_tick(900, new_day=True)
    assert event == "new_day"
    with d.STATE.lock:
        assert d.STATE.mention_daily == {}
        assert d.STATE.mention_ts    == {}
        assert d.STATE.mention_market_opened is False


def test_market_open_window_fires_at_930():
    event, _ = _run_one_tick(930)
    assert event == "market_open"
    with d.STATE.lock:
        assert d.STATE.mention_market_opened is True
        assert d.STATE.mention_daily == {}


def test_market_open_window_fires_at_935():
    event, _ = _run_one_tick(935)
    assert event == "market_open"


def test_market_open_does_not_fire_before_930():
    event, _ = _run_one_tick(929)
    assert event == "noop"


def test_market_open_idempotent():
    # First tick at 932 should fire; second at 933 should be noop.
    _run_one_tick(932)
    with d.STATE.lock:
        d.STATE.mention_daily["TEST"] = 9   # simulate new mentions
    event, _ = _run_one_tick(933)
    assert event == "noop"
    with d.STATE.lock:
        assert d.STATE.mention_daily.get("TEST") == 9  # unchanged


def test_market_close_window_fires_at_1605():
    event, fired = _run_one_tick(1605, close_reset_fired=False)
    assert event == "market_close"
    assert fired is True
    with d.STATE.lock:
        assert d.STATE.mention_daily == {}


def test_market_close_window_fires_at_1610():
    event, fired = _run_one_tick(1610, close_reset_fired=False)
    assert event == "market_close"
    assert fired is True


def test_market_close_does_not_fire_before_1605():
    event, _ = _run_one_tick(1604, close_reset_fired=False)
    assert event == "noop"


def test_market_close_idempotent():
    event, fired = _run_one_tick(1607, close_reset_fired=True)
    assert event == "noop"
    assert fired is True


def test_close_flag_cleared_after_1610():
    event, fired = _run_one_tick(1615, close_reset_fired=True)
    assert event == "close_flag_cleared"
    assert fired is False
