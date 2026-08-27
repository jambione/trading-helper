"""A failed close must not strand the position with its ratchet switched off.

Every exit sets closing_reason and THEN calls close_out. When the broker call
returns no order id the position is still open, still held, and permanently
flagged — and apply_local_trail skipped any position carrying that flag, so
the trailing stop was switched off for the life of the trade.

Observed 2026-08-27: IOVA held closing_reason='stale_data' with
close_order_id=None while its shelf sat at 8.1537 against its own 8.19 fill
and price traded at 8.27. Read from outside as "the ratchet never moves".
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def _stranded(**over):
    p = {
        "entry_confirmed": True, "entry_price": 8.19, "risk_per_share": 0.409,
        "last_seen_price": 8.27, "local_stop_price": 8.1537, "mfe_r": 0.306,
        "entry_time": NOW - 10_000,
        "closing_reason": "stale_data", "close_order_id": None,
    }
    p.update(over)
    return p


def _cfg(monkeypatch, wait=30.0):
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_stranded_close_sec": wait, "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.15, "ai_local_trail_be_at_r": 0.5,
        "ai_local_trail_be_at_pct": 0.6, "ai_local_trail_arm_r": 0.15,
        "ai_local_trail_min_give_px": 0.06, "ai_shelf_trace_sec": 0,
    })
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_local_trail_enabled": True}.get(k, d))


# ── the flag is stamped, then cleared ────────────────────────────────────

def test_first_sighting_stamps_but_does_not_clear(monkeypatch):
    """A close that is merely in flight must not be interrupted."""
    _cfg(monkeypatch)
    p = _stranded()
    assert cp.unstrand_failed_close("AAA", p, NOW) is False
    assert p["closing_since"] == NOW
    assert p["closing_reason"] == "stale_data"


def test_it_clears_once_the_wait_has_passed(monkeypatch):
    _cfg(monkeypatch)
    p = _stranded(closing_since=NOW - 31)
    assert cp.unstrand_failed_close("AAA", p, NOW) is True
    assert "closing_reason" not in p
    assert "closing_since" not in p


def test_a_close_that_actually_happened_is_left_alone(monkeypatch):
    """close_order_id present means the exit worked — do not resurrect it."""
    _cfg(monkeypatch)
    p = _stranded(close_order_id="abc", closing_since=NOW - 999)
    assert cp.unstrand_failed_close("AAA", p, NOW) is False
    assert p["closing_reason"] == "stale_data"


def test_a_position_not_closing_is_untouched(monkeypatch):
    _cfg(monkeypatch)
    p = _stranded(closing_reason=None)
    assert cp.unstrand_failed_close("AAA", p, NOW) is False


def test_zero_disables_clearing(monkeypatch):
    """The old behaviour, kept reachable — it strands forever."""
    _cfg(monkeypatch, wait=0.0)
    p = _stranded(closing_since=NOW - 9999)
    assert cp.unstrand_failed_close("AAA", p, NOW) is False
    assert p["closing_reason"] == "stale_data"


# ── the shelf keeps raising while flagged ────────────────────────────────

def test_the_shelf_still_raises_under_a_closing_flag(monkeypatch):
    """The whole point. Protection that switches off when an exit is attempted
    vanishes exactly when the close fails — the one case it was needed for."""
    _cfg(monkeypatch)
    import time as _t
    p = _stranded(closing_since=_t.time())    # stamped, not yet expired
    before = p["local_stop_price"]
    _ch, closed = cp.apply_local_trail("AAA", p, None, [], {})
    assert closed is False
    assert cp._num(p["local_stop_price"]) > before


def test_it_cannot_sell_again_while_flagged(monkeypatch):
    """A second close_out on a position already closing is a duplicate order.

    closing_since is real-clock here because apply_local_trail lets
    unstrand_failed_close read time.time() itself — a fixture timestamp from
    the distant past would look stranded and clear the flag instead."""
    import time as _t
    _cfg(monkeypatch)
    p = _stranded(closing_since=_t.time())
    exit_why = {}
    # A trigger far under the shelf: without the guard this is a sale.
    _ch, closed = cp.apply_local_trail("AAA", p, 1.00, [], exit_why)
    assert closed is False
    assert not exit_why


def test_an_unflagged_position_still_sells(monkeypatch):
    """The half that must not regress."""
    _cfg(monkeypatch)
    p = _stranded(closing_reason=None)
    _ch, closed = cp.apply_local_trail("AAA", p, 1.00, [], {})
    assert closed is True
