"""Risk-sized long plans and ratchet helpers."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from desk_risk import (  # noqa: E402
    next_phase,
    plan_long,
    stop_for_phase,
    trade_r,
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


def test_trade_r():
    assert trade_r(-50, 100) == -0.5
    assert trade_r(200, 100) == 2.0
