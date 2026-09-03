"""Risk-sized long plans and ratchet helpers."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from desk_risk import (  # noqa: E402
    cap_long_qty,
    equity_book_limits,
    limits_from_cfg,
    next_phase,
    open_book_notional,
    plan_long,
    position_notional,
    size_long_from_free_equity,
    stop_for_phase,
    trade_r,
    trail_stop_level,
    unrealized_r,
)
from desk_book import (  # noqa: E402
    empty_book,
    is_halted,
    manage_open,
    note_close,
    register_open,
)


def test_plan_long_tight_stop_small_size():
    # $100k equity, 0.35% risk = $350; stop 0.40% of $20 = $0.08 → 4375 sh
    # but notional would be huge; cap with max_notional
    p = plan_long(
        20.0, equity=100_000, risk_pct=0.35, stop_pct=0.40, reward_r=2.0,
        max_notional=2000,
    )
    assert p is not None
    assert p.qty >= 1
    assert p.qty * p.entry <= 2000 + 20  # floor effect
    assert p.stop < p.entry < p.target
    assert abs(p.r_per_share - (p.entry - p.stop)) < 1e-6
    # target = entry + 2R
    assert abs(p.target - (p.entry + 2 * p.r_per_share)) < 0.02


def test_plan_long_rejects_bad():
    assert plan_long(0, equity=100_000) is None
    assert plan_long(10, equity=0) is None


def test_unrealized_and_phases():
    assert abs(unrealized_r(10, 0.1, 10.2) - 2.0) < 1e-9
    assert next_phase(0.5) == "initial"
    assert next_phase(1.0) == "be"
    assert next_phase(2.5) == "locked"
    assert stop_for_phase("be", entry=10.0, initial_stop=9.96, r_per_share=0.04) == 10.0
    assert stop_for_phase("locked", entry=10.0, initial_stop=9.96, r_per_share=0.04) == 10.04


def test_session_halt_after_loss_r(tmp_path):
    book = empty_book("2026-08-04")
    book = register_open(
        book, symbol="AAA",
        plan={"entry": 10, "stop": 9.96, "target": 10.08, "qty": 10,
              "risk_dollars": 100, "r_per_share": 0.04},
        buy_order_id="x",
    )
    book = note_close(book, "AAA", realized_pnl=-100, daily_loss_r=2.0)
    assert book["session_r"] == -1.0
    assert not is_halted(book, 2.0)

    book = register_open(
        book, symbol="BBB",
        plan={"entry": 10, "stop": 9.96, "target": 10.08, "qty": 10,
              "risk_dollars": 100, "r_per_share": 0.04},
        buy_order_id="y",
    )
    book = note_close(book, "BBB", realized_pnl=-150, daily_loss_r=2.0)
    # -1 + -1.5 = -2.5
    assert book["session_r"] == -2.5
    assert is_halted(book, 2.0)
    assert book["halted"] is True


def test_manage_ratchet_to_be():
    book = empty_book("2026-08-04")
    book = register_open(
        book, symbol="XYZ",
        plan={
            "entry": 10.0, "stop": 9.96, "target": 10.08,
            "qty": 5, "risk_dollars": 20, "r_per_share": 0.04,
        },
        buy_order_id="1",
    )
    calls = []

    def rep(sym, stop):
        calls.append((sym, stop))
        return {"ok": True, "order_id": "s1"}

    live = {"XYZ": {"current": 10.05, "avg_entry": 10.0, "pl": 0.25}}
    # +1.25R → BE
    book, ev = manage_open(
        book, live_positions=live, cfg={"be_at_r": 1.0, "lock_at_r": 2.0},
        replace_stop_fn=rep,
    )
    assert any(e.get("phase") == "be" for e in ev)
    assert calls and calls[0][1] == 10.0
    assert book["positions"]["XYZ"]["phase"] == "be"


def _trailing_book():
    """XYZ long: entry 10.00, 1R = $0.04, initial stop 9.96."""
    return register_open(
        empty_book("2026-08-04"), symbol="XYZ",
        plan={
            "entry": 10.0, "stop": 9.96, "target": 10.08,
            "qty": 5, "risk_dollars": 20, "r_per_share": 0.04,
        },
        buy_order_id="1",
    )


_TRAIL_CFG = {"be_at_r": 1.0, "lock_at_r": 2.0, "trail_r": 1.0,
              "trail_step_r": 0.10}


def _tick(book, price, calls, cfg=None):
    def rep(sym, stop):
        calls.append((sym, stop))
        return {"ok": True, "order_id": "s1"}

    return manage_open(
        book,
        live_positions={"XYZ": {"current": price, "avg_entry": 10.0}},
        cfg=cfg or _TRAIL_CFG,
        replace_stop_fn=rep,
    )


def test_trail_stop_level_peak_minus_r_with_be_floor():
    # +3R peak, 1R trail → give back 1R, sit at +2R.
    assert trail_stop_level(
        entry=10.0, peak=10.12, r_per_share=0.04, trail_r=1.0) == 10.08
    # Breakeven floor: peak-1R is under entry early in the trade.
    assert trail_stop_level(
        entry=10.0, peak=10.02, r_per_share=0.04, trail_r=1.0) == 10.0
    # No basis to compute.
    assert trail_stop_level(entry=0, peak=10.0, r_per_share=0.04) is None
    assert trail_stop_level(entry=10.0, peak=10.1, r_per_share=0) is None


def test_trail_entry_matches_old_locked_stop():
    """At exactly lock_at the trail must land where "locked" used to: entry+1R.

    The fix is additive past this point, so a change here would mean the
    ratchet now banks *less* at the moment of lock than it did before.
    """
    book, calls = _trailing_book(), []
    book, ev = _tick(book, 10.08, calls)  # +2.0R
    assert calls[-1][1] == 10.04         # entry + 1R
    assert book["positions"]["XYZ"]["stop"] == 10.04
    assert any(e.get("phase") == "r_trail" for e in ev)


def test_trail_keeps_ratcheting_past_lock():
    """The regression this exists for: the stop must follow the peak up.

    Before, +2R wrote entry+1R, set phase "locked", and the branch excluded
    that phase — so a run to +8R still exited at +1R.
    """
    book, calls = _trailing_book(), []
    book, _ = _tick(book, 10.08, calls)   # +2R  → stop 10.04
    book, _ = _tick(book, 10.20, calls)   # +5R  → stop 10.16
    book, _ = _tick(book, 10.36, calls)   # +9R  → stop 10.32

    assert [c[1] for c in calls] == [10.04, 10.16, 10.32]
    pos = book["positions"]["XYZ"]
    assert pos["stop"] == 10.32
    assert pos["peak"] == 10.36
    assert pos["phase"] == "r_trail"


def test_trail_is_raise_only_on_pullback():
    book, calls = _trailing_book(), []
    book, _ = _tick(book, 10.36, calls)   # +9R → stop 10.32
    assert calls[-1][1] == 10.32

    n_before = len(calls)
    book, ev = _tick(book, 10.10, calls)  # pulls back to +2.5R
    # Peak is remembered, so the stop is neither lowered nor rewritten.
    assert len(calls) == n_before
    assert not ev
    pos = book["positions"]["XYZ"]
    assert pos["stop"] == 10.32
    assert pos["peak"] == 10.36


def test_trail_step_gate_suppresses_penny_churn():
    """Each move is a cancel + submit; a sub-step improvement must not fire."""
    book, calls = _trailing_book(), []
    book, _ = _tick(book, 10.08, calls)   # +2R → stop 10.04
    n_before = len(calls)

    # Peak up $0.001 → trail improves far less than trail_step_r (0.10 × $0.04).
    book, ev = _tick(book, 10.081, calls)
    assert len(calls) == n_before
    assert not ev
    assert book["positions"]["XYZ"]["stop"] == 10.04


def test_broker_trail_still_owns_the_stop_when_configured():
    """trail_pct > 0 hands off to the broker; the R-trail must not fight it."""
    book = _trailing_book()
    reps, trails = [], []

    def rep(sym, stop):
        reps.append((sym, stop))
        return {"ok": True}

    def trail(sym, pct):
        trails.append((sym, pct))
        return {"ok": True}

    cfg = dict(_TRAIL_CFG, trail_pct=2.5)
    book, _ = manage_open(
        book, live_positions={"XYZ": {"current": 10.08, "avg_entry": 10.0}},
        cfg=cfg, replace_stop_fn=rep, trail_fn=trail,
    )
    assert trails == [("XYZ", 2.5)]
    assert book["positions"]["XYZ"]["phase"] == "trail"

    # Next tick: broker owns it, so no software stop rewrite.
    book, _ = manage_open(
        book, live_positions={"XYZ": {"current": 10.40, "avg_entry": 10.0}},
        cfg=cfg, replace_stop_fn=rep, trail_fn=trail,
    )
    assert reps == []
    assert trails == [("XYZ", 2.5)]


def test_trade_r():
    assert trade_r(-50, 100) == -0.5
    assert trade_r(200, 100) == 2.0


def test_equity_book_limits_small_account_one_slot():
    lim = equity_book_limits(250.0, max_positions=8, max_position_pct=8.0)
    assert lim.max_positions == 1
    assert lim.dollar_cap == 250.0
    assert lim.max_position_pct == pytest.approx(100.0)


def test_equity_book_limits_grows_slots_and_dollars():
    a = equity_book_limits(250.0)
    b = equity_book_limits(500.0)
    c = equity_book_limits(10_000.0)
    assert a.max_positions == 1
    assert b.max_positions == 2
    assert c.max_positions == 8
    # Risk/notional room grows with equity once 8% exceeds one slot.
    assert c.dollar_cap > a.dollar_cap
    assert c.dollar_cap == pytest.approx(800.0)  # 8% of $10k
    assert b.dollar_cap == pytest.approx(250.0)  # still the $250 floor


def test_equity_book_limits_slot_equity_zero_disables_scale():
    lim = equity_book_limits(
        250.0, max_positions=8, max_position_pct=8.0, slot_equity=0.0)
    assert lim.max_positions == 8
    assert lim.max_position_pct == pytest.approx(8.0)


def test_limits_from_cfg_reads_desk_keys():
    lim = limits_from_cfg(250.0, {
        "ai_max_positions": 8,
        "ai_max_position_pct": 8.0,
        "ai_position_slot_equity": 250.0,
    })
    assert lim.max_positions == 1
    assert lim.dollar_cap == 250.0


def test_cap_long_qty_promotes_zero_to_one_share_when_affordable():
    # 1% of $250 cannot buy a $20 name with a $1 stop, but $20 < $250 cap.
    assert cap_long_qty(0, equity=250.0, price=20.0, max_position_pct=100.0) == 1


def test_cap_long_qty_lets_risk_size_grow():
    # $10 stock, 100% cap on $250 → 25 sh ceiling; risk size 5 stays 5.
    assert cap_long_qty(5, equity=250.0, price=10.0, max_position_pct=100.0) == 5
    # Same name at $10k / 8% → $800 / $10 = 80 sh ceiling; 40 stays 40.
    assert cap_long_qty(40, equity=10_000.0, price=10.0, max_position_pct=8.0) == 40
    # Tight stop that wants 200 sh is clamped to 80.
    assert cap_long_qty(200, equity=10_000.0, price=10.0, max_position_pct=8.0) == 80


def test_position_notional_prefers_live_mv():
    assert position_notional({"mkt_val": 80.0, "qty": 2, "avg_entry": 10.0}) == 80.0
    assert position_notional({"qty": 2, "current": 11.0, "avg_entry": 10.0}) == 22.0
    assert position_notional({"total_qty": 3, "entry_price": 10.0}) == 30.0
    assert position_notional({"qty": 0, "mkt_val": 0}) == 0.0


def test_open_book_notional_skips_closed_and_symbol():
    rows = {
        "AAA": {"qty": 2, "mkt_val": 80.0},
        "BBB": {"qty": 1, "avg_entry": 40.0},
        "CCC": {"qty": 4, "mkt_val": 50.0, "closing_reason": "stop"},
        "DDD": {"qty": 0, "mkt_val": 12.0},
    }
    notional, count = open_book_notional(rows)
    assert count == 2
    assert notional == 120.0
    notional, count = open_book_notional(rows, skip_symbol="aaa")
    assert count == 1
    assert notional == 40.0


def test_free_equity_empty_book_buys_multiple_shares_not_one_percent_risk():
    """~$238, 3 slots, $26 name, $1 stop → ~3 shares, not 1 from 1% risk."""
    plan = size_long_from_free_equity(
        account_equity=238.0,
        price=26.0,
        stop=25.0,
        risk_pct=1.0,
        open_notional=0.0,
        open_count=0,
        max_positions=3,
        slot_equity=60.0,
        max_position_pct=8.0,
        max_open_risk_pct=5.0,
    )
    # 238/3/26 = 3.05 → 3 shares. 1% risk alone is 2.38/1 = 2.
    assert plan.remaining_slots == 3
    assert plan.free_equity == 238.0
    assert plan.notional_qty == 3
    assert plan.risk_qty == 2
    assert plan.qty == 3


def test_free_equity_third_entry_sizes_off_leftover_not_full_book():
    """Two opens tying up capital: third ticket uses leftover / 1 slot."""
    empty = size_long_from_free_equity(
        account_equity=238.0,
        price=26.0,
        stop=25.0,
        risk_pct=1.0,
        open_notional=0.0,
        open_count=0,
        max_positions=3,
        slot_equity=60.0,
        max_position_pct=8.0,
    )
    leftover = size_long_from_free_equity(
        account_equity=238.0,
        price=26.0,
        stop=25.0,
        risk_pct=1.0,
        open_notional=180.0,
        open_count=2,
        max_positions=3,
        slot_equity=60.0,
        max_position_pct=8.0,
    )
    assert empty.qty == 3
    assert leftover.remaining_slots == 1
    assert leftover.free_equity == 58.0
    # 58/26 = 2; must not reuse the full $238/3 slot.
    assert leftover.qty == 2
    assert leftover.qty < empty.qty


def test_free_equity_large_account_keeps_risk_size_then_8pct_cap():
    """$50k / 8 slots / 25% cap: 1% of equity with $2.50 stop is 200 sh."""
    plan = size_long_from_free_equity(
        account_equity=50_000.0,
        price=40.5,
        stop=38.0,
        risk_pct=1.0,
        open_notional=0.0,
        open_count=0,
        max_positions=8,
        slot_equity=250.0,
        max_position_pct=25.0,
    )
    # Slot would be 50000/8/40.5 = 154; risk 200 is larger and under 25%.
    assert plan.notional_qty == 154
    assert plan.risk_qty == 200
    assert plan.qty == 200


def test_free_equity_downsizes_to_buying_power():
    plan = size_long_from_free_equity(
        account_equity=238.0,
        price=26.0,
        stop=25.0,
        risk_pct=1.0,
        open_notional=0.0,
        open_count=0,
        max_positions=3,
        slot_equity=60.0,
        max_position_pct=8.0,
        buying_power=40.0,
    )
    assert plan.qty == 1
    assert "buying_power" in plan.capped_by


def test_free_equity_wide_stop_hits_open_risk_ceiling_not_account_blowup():
    """3 shares of $26 with a $5 stop is $15 risk (>5% of $238) → shrink."""
    plan = size_long_from_free_equity(
        account_equity=238.0,
        price=26.0,
        stop=21.0,
        risk_pct=1.0,
        open_notional=0.0,
        open_count=0,
        max_positions=3,
        slot_equity=60.0,
        max_position_pct=8.0,
        max_open_risk_pct=5.0,
    )
    # 5% of 238 = $11.90 / $5 = 2 shares.
    assert plan.notional_qty == 3
    assert plan.qty == 2
    assert "open_risk" in plan.capped_by

