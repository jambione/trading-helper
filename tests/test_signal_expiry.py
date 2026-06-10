"""
test_signal_expiry.py — unit tests for TickerState.is_expired() and
time_left_s(), covering all four expiry conditions.

Requires the full venv (signal_engine imports pandas/numpy).
Run:
    venv/bin/python -m pytest tests/test_signal_expiry.py -q
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")
TickerState = se.TickerState


def test_in_position_never_expires():
    ts = TickerState("NVDA")
    ts.added_ts    = time.time() - 99999
    ts.in_position = True
    assert not ts.is_expired()


def test_cold_ticker_expires_after_expiry_cold():
    ts = TickerState("NVDA")
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 1)
    assert ts.is_expired()


def test_cold_ticker_not_yet_expired():
    ts = TickerState("NVDA")
    ts.ever_positive_hist = False
    ts.added_ts = time.time() - (se.EXPIRY_COLD - 5)
    assert not ts.is_expired()


def test_warm_ticker_survives_past_expiry_cold():
    ts = TickerState("NVDA")
    ts.ever_positive_hist = True
    ts.added_ts = time.time() - (se.EXPIRY_COLD + 1)
    assert se.EXPIRY_COLD < se.EXPIRY_WARM, "constant sanity check"
    assert not ts.is_expired()


def test_warm_ticker_expires_after_expiry_warm():
    ts = TickerState("NVDA")
    ts.ever_positive_hist = True
    ts.added_ts = time.time() - (se.EXPIRY_WARM + 1)
    assert ts.is_expired()


def test_cooled_ticker_expires_after_expiry_cooled():
    ts = TickerState("NVDA")
    ts.cooled_at = time.time() - (se.EXPIRY_COOLED + 1)
    assert ts.is_expired()


def test_cooled_ticker_not_expired_before_window():
    ts = TickerState("NVDA")
    ts.cooled_at = time.time() - 1
    assert not ts.is_expired()


def test_time_left_uses_cooled_window_not_warm_window():
    ts = TickerState("NVDA")
    ts.ever_positive_hist = True   # without the fix this would give EXPIRY_WARM countdown
    ts.cooled_at = time.time() - (se.EXPIRY_COOLED // 2)
    left = ts.time_left_s()
    assert 0 < left < se.EXPIRY_COOLED, (
        f"time_left_s() should reflect EXPIRY_COOLED window, got {left:.1f}s"
    )
    assert not ts.is_expired()   # time_left > 0 must be consistent with is_expired()=False


def test_time_left_zero_when_cooled_and_expired():
    ts = TickerState("NVDA")
    ts.cooled_at = time.time() - (se.EXPIRY_COOLED + 60)
    assert ts.time_left_s() == 0.0
    assert ts.is_expired()   # zero time left must be consistent with is_expired()=True


# ── decay_mentions ────────────────────────────────────────────────────────────

def test_decay_transitions_hot_to_cooled():
    ts = TickerState("NVDA")
    ts.is_hot = True
    ts.went_hot = True
    ts.mention_history = [(time.time(), 1)]   # one fresh mention
    ts.mention_velocity = 1

    # Expire the window by backdating the mention
    ts.mention_history = [(time.time() - se.PRIORITY_WINDOW_SECONDS - 1, 1)]
    ts.decay_mentions()

    assert not ts.is_hot
    assert ts.cooled_at is not None


def test_decay_does_not_set_cooled_when_still_hot():
    ts = TickerState("NVDA")
    ts.is_hot = True
    # All mentions fresh — velocity stays above threshold
    ts.mention_history = [(time.time(), se.PRIORITY_MENTIONS)] * 3
    ts.mention_velocity = se.PRIORITY_MENTIONS * 3
    ts.decay_mentions()

    assert ts.is_hot
    assert ts.cooled_at is None


def test_decay_does_not_set_cooled_when_in_position():
    ts = TickerState("NVDA")
    ts.is_hot = True
    ts.in_position = True
    ts.mention_history = [(time.time() - se.PRIORITY_WINDOW_SECONDS - 1, 1)]
    ts.mention_velocity = 1
    ts.decay_mentions()

    # cooled_at must not be set when in a position — is_expired() returns False
    assert ts.cooled_at is None
    assert not ts.is_expired()
