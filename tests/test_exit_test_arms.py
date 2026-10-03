"""Exit-cost A/B: passive local-trail exits rest a sell limit and are settled on later ticks.

The arm must never block the shelf tick, never apply to anything but a local-trail
exit, and always end with the position sold: filled at the limit, or crossed at
market when the wait is over, the print breaks the floor, or the broker kills the limit.
"""
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def test_limits_per_arm():
    assert cp.exit_test_limit("market", 10.00, 10.04) is None
    # 1c spread: mid rounded UP is the ask
    assert cp.exit_test_limit("mid", 10.00, 10.01) == 10.01
    assert cp.exit_test_limit("mid", 10.00, 10.03) == 10.02
    assert cp.exit_test_limit("mid", 25.42, 25.47) == 25.45
    assert cp.exit_test_limit("mid", 0, 10.01) is None          # no usable quote
    assert cp.exit_test_limit("mid", 10.02, 10.01) is None


def test_rotation_and_off(monkeypatch):
    monkeypatch.setattr(cp, "_EXIT_TEST_N", [0])
    assert cp.exit_test_plan({"ai_exit_test_arms": ""}, 1, 2) is None
    cfg = {"ai_exit_test_arms": "market,mid"}
    assert [cp.exit_test_plan(cfg, 10.0, 10.04)["arm"] for _ in range(4)] == ["market", "mid"] * 2
    p = cp.exit_test_plan({"ai_exit_test_arms": "mid"}, 0, 10.0)
    assert p["arm"] == "mid" and p["limit"] is None and p["fallback"] is True


class Broker:
    """Scripted order replies; records close_out calls."""

    def __init__(self, monkeypatch, limit_reply, close_reply=None, market_reply=None):
        self.limit_reply, self.market_reply = limit_reply, market_reply
        self.close_reply = close_reply or {"ok": True, "order_id": "M"}
        self.closed = 0
        self.limits = []
        monkeypatch.setitem(sys.modules, "alpaca_trader", types.SimpleNamespace(
            get_order=self._get, close_out=self._close,
            cancel_open_orders=lambda *a, **k: None,
            place_limit_sell=self._lim))

    def _get(self, oid):
        return self.market_reply if oid == "M" else self.limit_reply

    def _close(self, t, *a, **k):
        self.closed += 1
        return self.close_reply

    def _lim(self, t, qty, px, **k):
        self.limits.append((t, qty, px, k))
        return {"ok": True, "order_id": "L", "qty": 100.0}


def _pending(**over):
    p = {"entry_confirmed": True, "closing_reason": "local_trail", "close_order_id": "L",
         "exit_test": {"arm": "mid", "limit": 10.02, "bid": 10.0, "ask": 10.04},
         "exit_test_pending": {"order_id": "L", "limit": 10.02, "qty": 100.0, "t0": NOW,
                               "deadline": NOW + 10, "floor": 9.97}}
    p.update(over)
    return p


def test_filled_limit_finishes_without_crossing(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.FILLED", "filled_qty": 100, "filled_avg_price": 10.02})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 3) is True
    assert b.closed == 0 and "exit_test_pending" not in p
    assert p["exit_test"]["fill"] == 10.02 and p["exit_test"]["crossed_qty"] == 0.0
    assert p["close_order_id"] == "L"


def test_waits_while_not_due(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 3) is False
    assert b.closed == 0 and "exit_test_pending" in p


def test_deadline_crosses_at_market(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 10) is True
    assert b.closed == 1 and "exit_test_pending" not in p
    assert p["exit_test"]["cross_why"] == "deadline"
    assert p["close_order_id"] == "M" and p["exit_test"]["market_order_id"] == "M"


def test_floor_break_crosses_before_the_deadline(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 9.96, NOW + 1) is True
    assert b.closed == 1 and p["exit_test"]["cross_why"] == "floor"


def test_broker_killed_limit_crosses(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.REJECTED", "filled_qty": 0})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 1) is True
    assert b.closed == 1 and p["exit_test"]["cross_why"] == "dead"


def test_mostly_passive_keeps_the_limit_as_the_exit_order(monkeypatch):
    Broker(monkeypatch, {"status": "OrderStatus.PARTIALLY_FILLED", "filled_qty": 70, "filled_avg_price": 10.02})
    p = _pending()
    cp._exit_test_settle("AAA", p, 10.01, NOW + 10)
    assert p["close_order_id"] == "L" and p["exit_test"]["passive_qty"] == 70


def test_failed_cross_retries_next_tick(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0},
               close_reply={"ok": False, "order_id": None, "note": "error"})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 10) is True
    assert "exit_test_pending" in p and b.closed == 1
    b.close_reply = {"ok": True, "order_id": "M"}
    b.limit_reply = {"status": "OrderStatus.CANCELED", "filled_qty": 0}
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 11) is True
    assert b.closed == 2 and "exit_test_pending" not in p


def test_filled_during_the_cancel_race_is_not_resold(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0},
               close_reply={"ok": False, "order_id": None, "note": "no position (canceled 0 open orders)"})
    p = _pending()
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 10) is True
    assert b.closed == 1 and "exit_test_pending" not in p and p["close_order_id"] == "L"


def test_another_exit_taking_over_drops_the_test(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _pending(close_order_id="EOD")
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 1) is True
    assert b.closed == 0 and "exit_test_pending" not in p and p["exit_test"]["superseded"] is True


def test_outcome_blends_both_legs(monkeypatch):
    Broker(monkeypatch, {"status": "OrderStatus.CANCELED", "filled_qty": 40, "filled_avg_price": 10.02},
           market_reply={"status": "OrderStatus.FILLED", "filled_qty": 60, "filled_avg_price": 10.00})
    p = _pending()
    cp._exit_test_settle("AAA", p, 10.01, NOW + 10)
    xt = cp._exit_test_final(p)
    assert xt["passive_qty"] == 40 and xt["crossed_qty"] == 60
    assert abs(xt["fill"] - 10.008) < 1e-9


def test_outcome_reads_a_limit_that_filled_before_settle(monkeypatch):
    Broker(monkeypatch, {"status": "OrderStatus.FILLED", "filled_qty": 100, "filled_avg_price": 10.02})
    xt = cp._exit_test_final(_pending())
    assert xt["fill"] == 10.02 and xt["passive_qty"] == 100


# ── wired into apply_local_trail ──────────────────────────────────────────

def _cfg(monkeypatch, arms):
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_local_trail_enabled": True, "ai_local_trail_give_r": 0.15, "ai_local_trail_be_at_r": 0.5,
        "ai_local_trail_be_at_pct": 0.6, "ai_local_trail_arm_r": 0.15,
        "ai_local_trail_min_give_px": 0.06, "ai_shelf_trace_sec": 0,
        "ai_exit_test_arms": arms, "ai_exit_test_cross_sec": 10.0, "ai_exit_test_floor_pct": 0.5})
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {"ai_local_trail_enabled": True}.get(k, d))
    monkeypatch.setattr(cp, "_premarket_working_sell_on", lambda now=None: False)
    monkeypatch.setattr(cp, "_premarket_book", lambda s: (10.00, 10.04))
    monkeypatch.setattr(cp, "_EXIT_TEST_N", [0])


def _open_pos():
    return {"entry_confirmed": True, "entry_price": 10.20, "risk_per_share": 0.4,
            "last_seen_price": 10.05, "local_stop_price": 10.10, "mfe_r": 0.5,
            "entry_time": time.time() - 600}


def test_trail_hit_on_the_mid_arm_rests_a_limit_and_does_not_block(monkeypatch):
    _cfg(monkeypatch, "mid")
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p, why = _open_pos(), {}
    _ch, closed = cp.apply_local_trail("AAA", p, 9.00, [], why)
    assert closed is True and b.closed == 0
    assert b.limits and b.limits[0][2] == 10.02 and b.limits[0][3]["time_in_force"] == "day"
    assert p["close_order_id"] == "L" and p["closing_reason"] == "local_trail"
    assert p["exit_test"]["arm"] == "mid" and p["exit_test_pending"]["order_id"] == "L"
    assert why["AAA"] == "local_trail"


def test_trail_hit_on_the_market_arm_sells_at_market(monkeypatch):
    _cfg(monkeypatch, "market")
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW"})
    p = _open_pos()
    _ch, closed = cp.apply_local_trail("AAA", p, 9.00, [], {})
    assert closed is True and b.closed == 1 and not b.limits
    assert p["exit_test"]["arm"] == "market" and "exit_test_pending" not in p


def test_off_by_default_leaves_no_trace(monkeypatch):
    _cfg(monkeypatch, "")
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW"})
    p = _open_pos()
    cp.apply_local_trail("AAA", p, 9.00, [], {})
    assert b.closed == 1 and not b.limits and "exit_test" not in p


def test_pending_exit_settles_on_the_next_tick(monkeypatch):
    _cfg(monkeypatch, "mid")
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _open_pos()
    cp.apply_local_trail("AAA", p, 9.00, [], {})
    p["exit_test_pending"]["deadline"] = time.time() - 1      # the wait is over
    p["closing_since"] = time.time()
    _ch, closed = cp.apply_local_trail("AAA", p, 9.99, [], {})
    assert b.closed == 1 and "exit_test_pending" not in p and p["exit_test"]["cross_why"] == "deadline"
    assert closed is False                                     # raise-only while closing: no second sell
