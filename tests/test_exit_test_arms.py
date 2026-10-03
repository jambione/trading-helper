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
    monkeypatch.setattr(cp, "_rth_now", lambda now: True)


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


def test_outside_rth_a_trail_hit_sells_at_market_with_no_exit_test(monkeypatch):
    _cfg(monkeypatch, "mid")
    monkeypatch.setattr(cp, "_rth_now", lambda now: False)
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _open_pos()
    _ch, closed = cp.apply_local_trail("AAA", p, 9.00, [], {})
    assert closed is True and b.closed == 1 and not b.limits
    assert "exit_test" not in p and "exit_test_pending" not in p
    assert cp._EXIT_TEST_N == [0]                              # the rotation did not step


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


# ── the outcome path (manage_open_positions pass 1) ───────────────────────

def _manage_stub(monkeypatch, tmp_path, fills, qtys, **pos_fields):
    """A flat position seeded through test_ai_positions' harness, with filled_qty per order."""
    from test_ai_positions import _StubBrokerManage, _seed_state, _outcomes_path

    class Stub(_StubBrokerManage):
        def get_order(self, order_id):
            out = super().get_order(order_id)
            if order_id in qtys:
                out["filled_qty"] = qtys[order_id]
            return out

    monkeypatch.setattr(cp, "refresh_open_position_quotes", lambda *_a, **_k: 0)
    _seed_state(tmp_path, monkeypatch, tranche_a_target_order_id=None,
                tranche_b_stop_order_id=None, closing_reason="local_trail",
                last_seen_price=40.4, **pos_fields)
    stub = Stub(position_open=False, fills=fills, order_status="filled")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    return lambda: __import__("json").loads(_outcomes_path(tmp_path).read_text().strip())


def test_split_exit_outcome_is_priced_at_the_blended_fill(tmp_path, monkeypatch):
    # 70 shares rested at 40.60, 30 crossed at 40.00: the limit is close_order_id
    # (the larger leg), so resolve_exit alone would book 40.60 for all 100.
    outcome = _manage_stub(
        monkeypatch, tmp_path, fills={"L": 40.60, "M": 40.00}, qtys={"L": 70, "M": 30},
        close_order_id="L",
        exit_test={"arm": "mid", "limit": 40.60, "passive_qty": 70.0, "passive_px": 40.60,
                   "market_order_id": "M", "cross_why": "deadline"})
    cp.manage_open_positions(now=1_000_010.0)
    o = outcome()
    assert abs(o["exit_price"] - 40.42) < 1e-9
    assert o["exit_test"]["crossed_qty"] == 30 and abs(o["exit_test"]["fill"] - 40.42) < 1e-9
    assert abs(o["realized_r_multiple"] - (40.42 - 40.5) / 2.5) < 1e-6


def test_market_arm_outcome_still_prices_from_the_close_order(tmp_path, monkeypatch):
    outcome = _manage_stub(
        monkeypatch, tmp_path, fills={"close_1": 40.30}, qtys={"close_1": 100},
        close_order_id="close_1", exit_test={"arm": "market", "limit": None})
    cp.manage_open_positions(now=1_000_010.0)
    assert outcome()["exit_price"] == 40.30


def test_pending_exit_still_settles_with_the_trail_turned_off(monkeypatch):
    _cfg(monkeypatch, "mid")
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0})
    p = _open_pos()
    cp.apply_local_trail("AAA", p, 9.00, [], {})
    assert "exit_test_pending" in p
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {"ai_local_trail_enabled": False}.get(k, d))
    p["exit_test_pending"]["deadline"] = time.time() - 1
    changed, closed = cp.apply_local_trail("AAA", p, 9.99, [], {})
    assert changed is True and closed is False
    assert b.closed == 1 and "exit_test_pending" not in p and p["exit_test"]["cross_why"] == "deadline"


def _resting(**over):
    xt = {"arm": "mid", "limit": 40.60, "bid": 40.5, "ask": 40.7}
    pend = {"order_id": "L", "limit": 40.60, "qty": 250.0, "t0": 1_000_000.0,
            "deadline": 1_000_010.0, "floor": 40.40}
    return dict(close_order_id="L", exit_test=xt, exit_test_pending=pend, **over)


def test_restart_resumes_a_pending_exit_through_manage(tmp_path, monkeypatch):
    # State file written mid-rest, process restarted: the next book tick crosses it.
    from test_ai_positions import _StubBrokerManage, _seed_state
    monkeypatch.setattr(cp, "refresh_open_position_quotes", lambda *_a, **_k: 0)
    monkeypatch.setattr(cp.time, "time", lambda: 1_000_020.0)
    _seed_state(tmp_path, monkeypatch, closing_reason="local_trail", last_seen_price=40.55,
                local_stop_price=40.6, **_resting())
    stub = _StubBrokerManage(position_open=True, current_price=40.55, order_status="new")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    cp.manage_open_positions(now=1_000_020.0)
    st = cp._load_state()["NVDA"]
    assert stub.closed == ["NVDA"]
    assert "exit_test_pending" not in st and st["exit_test"]["cross_why"] == "deadline"
    assert st["close_order_id"] == "close_1"


def test_eod_sweep_during_a_rest_does_not_book_the_limit_as_the_fill(tmp_path, monkeypatch):
    # liquidate_all cancelled the half-filled limit and sold the rest at market;
    # the position is flat before a settle tick ran.
    outcome = _manage_stub(monkeypatch, tmp_path, fills={"L": 40.60}, qtys={"L": 30},
                           **_resting())
    stub = sys.modules["alpaca_trader"]
    stub.order_status = "canceled"
    cp.manage_open_positions(now=1_000_005.0)
    xt = outcome()["exit_test"]
    assert xt.get("fill") is None and xt["superseded"] is True and xt["passive_qty"] == 30


def test_limit_filled_in_the_cancel_race_keeps_its_fill(monkeypatch):
    b = Broker(monkeypatch, {"status": "OrderStatus.NEW", "filled_qty": 0},
               close_reply={"ok": False, "order_id": None, "note": "no position (canceled 0 open orders)"})
    p = _pending()
    # Unfilled when settle looks, filled by the time close_out finds nothing to sell.
    orig_close = b._close

    def close(t, *a, **k):
        b.limit_reply = {"status": "OrderStatus.FILLED", "filled_qty": 100, "filled_avg_price": 10.02}
        return orig_close(t, *a, **k)
    sys.modules["alpaca_trader"].close_out = close
    assert cp._exit_test_settle("AAA", p, 10.01, NOW + 10) is True
    xt = cp._exit_test_final(p)
    assert xt["fill"] == 10.02 and xt["crossed_qty"] == 0.0 and xt["passive_qty"] == 100
    assert "market_order_id" not in xt and p["close_order_id"] == "L"
