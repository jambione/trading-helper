"""ai_exit_min_hold_sec: hold back discretionary exits, never protection.

Realized R is monotone in how long a trade was allowed to live (hold <10s
returns -0.088 R at a 2% win rate; hold >10m returns +0.055 R at 52%).
Survival is an outcome and cannot be chosen — the delay can.

The whole trade being made is that max loss becomes the 1R disaster stop
instead of the 0.10R shelf. So the two things that make the wait survivable
— the hard stop and the 15:50 flatten — must never be gated, and the
default must be off.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def _pos(age_sec):
    return {"entry_time": NOW - age_sec}


def test_off_by_default():
    """Shipped behaviour: every exit armed from the first tick."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_exit_min_hold_sec"] == 0
    assert cp.soft_exit_held_back(_pos(1), NOW, {}) is False


def test_young_fill_is_held_back():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(60), NOW, cfg) is True
    assert cp.soft_exit_held_back(_pos(1799), NOW, cfg) is True


def test_matured_fill_is_released():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(1800), NOW, cfg) is False
    assert cp.soft_exit_held_back(_pos(3600), NOW, cfg) is False


def test_zero_and_negative_never_hold():
    for v in (0, 0.0, -1, "", None):
        assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": v}) is False


def test_unknown_age_fails_open():
    """No entry_time must not read as 'young' and pin a position open."""
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back({}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": None}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": "junk"}, NOW, cfg) is False
    assert cp.soft_exit_held_back(None, NOW, cfg) is False


def test_garbage_config_fails_open():
    assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": "soon"}) is False


def test_it_gates_the_three_discretionary_exits():
    """Shelf, dead-trade and left-overbought — the desk's opinions.

    Asserted as a property rather than as three literal lines: the first
    version of this test pinned the exact one-line form of each condition
    and broke the moment the held-back branch was split out to be counted,
    which is a test failing on formatting rather than on behaviour.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    # 6 mentions total: the definition, FOUR gates, and one observational
    # read in the shadow logger. The logger reads the state and decides
    # nothing, which is the distinction worth pinning — if that count moves,
    # something new is either gating on the delay or has stopped recording it.
    #
    # The fourth is the MACD thesis break (2026-08-27). A withdrawn entry
    # thesis is a better reason to sell than a 6c wiggle, but it is still not
    # a reason to sell forty seconds after buying, so it is gated like the
    # rest — ai_exit_macd_liquidate_ignore_hold exempts it deliberately.
    assert src.count("soft_exit_held_back(pos") == 6
    assert src.count('"min_hold_active": soft_exit_held_back(pos, now)') == 1
    for which in ("local_trail", "left_overbought", "dead_trade"):
        assert f'_note_min_hold(pos, "{which}"' in src, (
            f"{which} suppression must be counted or GATE 1 has no mechanism")


def test_every_gated_exit_also_records_the_block():
    """A suppressed exit that logs nothing is an experiment with no readout."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    assert src.count("_note_min_hold(") == 5      # 4 call sites + the def
    # The MACD gate passes the reason through a variable rather than a
    # literal, so the label is macd_negative / macd_curl at runtime.
    assert "_note_min_hold(pos, _macd_why, now)" in src


def test_noting_a_block_counts_and_labels_without_deciding():
    pos = {"entry_time": NOW}
    cp._note_min_hold(pos, "local_trail", NOW)
    cp._note_min_hold(pos, "dead_trade", NOW + 1)
    assert pos["min_hold_blocks"] == 2
    assert pos["min_hold_last"] == "dead_trade"
    assert pos["min_hold_last_ts"] == NOW + 1


def test_noting_a_block_survives_a_corrupt_counter():
    pos = {"min_hold_blocks": "lots"}
    cp._note_min_hold(pos, "local_trail", NOW)
    assert pos["min_hold_blocks"] == 1


def test_noting_a_block_on_a_non_position_is_a_no_op():
    cp._note_min_hold(None, "local_trail", NOW)      # must not raise


def test_the_disaster_stop_is_never_gated():
    """1R stop and the 15:50 flatten are what make the wait survivable.

    They are placed with the broker and run through liquidate_all, neither
    of which consults soft_exit_held_back — pinned here because gating one
    of them later would turn a bounded loss into an unbounded one.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    i = src.index("def soft_exit_held_back")
    j = src.index("def _note_min_hold")
    body = src[i:j]
    for forbidden in ("entry_stop_price", "liquidate_all", "eod"):
        assert forbidden not in body, (
            f"{forbidden} must stay outside the min-hold gate")


def test_ratchet_sale_waits_for_min_hold(monkeypatch):
    """LAST through the shelf must not flatten until min-hold is over."""
    import sys
    import time
    import types

    closed = []
    stub = types.SimpleNamespace(
        cancel_open_orders=lambda *a, **k: None,
        close_out=lambda t: closed.append(t) or {"ok": True, "order_id": "x"},
    )
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_exit_min_hold_sec": 300,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_min_give_px": 0,
    })
    monkeypatch.setattr(cp, "_cfg_flag", lambda key, default=True: {
        "ai_local_trail_enabled": True,
    }.get(key, default))

    now = time.time()
    young = {
        "entry_confirmed": True,
        "entry_time": now - 60,
        "entry_price": 10.0,
        "entry_stop_price": 9.5,
        "stop_price": 9.5,
        "local_stop_price": 9.95,
        "last_seen_price": 9.94,
        "peak_price": 10.10,
        "risk_per_share": 0.5,
    }
    _ch, done = cp.apply_local_trail("AAA", young, 9.94, [], {})
    assert done is False
    assert young.get("closing_reason") is None
    assert closed == []
    assert young.get("min_hold_last") == "local_trail"
    assert int(young.get("min_hold_blocks") or 0) >= 1

    mature = dict(young, entry_time=now - 301, closing_reason=None,
                  min_hold_blocks=0, min_hold_last=None)
    _ch, done = cp.apply_local_trail("AAA", mature, 9.94, [], {})
    assert done is True
    assert mature.get("closing_reason") == "local_trail"
    assert closed == ["AAA"]


# ── the shelf may raise on a stale print; it may not sell on one ────────────
#
# The 0.25s tick did `px = _fresh_tape_px(ticker); if px is None: continue`,
# which skipped the RAISE along with the sale. Raising is monotonic and
# conservative — a stop moved up on a slightly old print is still a stop that
# only helps — while selling on a stale print is exactly what the freshness
# guard exists to prevent. Coupled, any name whose stream went quiet had its
# shelf frozen: IOVA held 8.1537 for a full minute on 2026-08-27 against a
# computed 8.21365, six cents of earned protection never banked, while SBET
# and IBRX ticked normally beside it.

def test_raise_only_banks_the_shelf_without_selling():
    pos = {
        "entry_confirmed": True, "entry_price": 8.19,
        "risk_per_share": 0.409, "last_seen_price": 8.275,
        "local_stop_price": 8.1537, "mfe_r": 0.306,
        "entry_time": NOW - 10_000,
    }
    events, exit_why = [], {}
    # A trigger far UNDER the shelf: without raise_only this is a sale.
    _ch, closed = cp.apply_local_trail(
        "AAA", pos, 1.00, events, exit_why, raise_only=True)
    assert closed is False, "raise_only must never sell"
    assert not exit_why
    assert cp._num(pos["local_stop_price"]) > 8.1537, "shelf must still raise"


def test_the_same_trigger_does_sell_without_raise_only():
    """The half that must not regress — raise_only is a mode, not a defusing."""
    pos = {
        "entry_confirmed": True, "entry_price": 8.19,
        "risk_per_share": 0.409, "last_seen_price": 8.275,
        "local_stop_price": 8.1537, "mfe_r": 0.306,
        "entry_time": NOW - 10_000,
    }
    _ch, closed = cp.apply_local_trail("AAA", pos, 1.00, [], {})
    assert closed is True


def test_the_stale_branch_raises_instead_of_skipping():
    """Source-pinned: the bug was a bare `continue`, and it is invisible in
    unit tests because the skip happens in the caller, not the shelf."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    i = src.index("px = _fresh_tape_px(ticker)")
    body = src[i:i + 1200]
    assert "raise_only=True" in body, "a stale tick must still bank the shelf"
    assert "apply_local_trail(" in body
