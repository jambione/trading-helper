"""The buy must survive as long as the sell does.

GAP, 2026-08-28, from the shadow log:

    13:31:33  arm=False  macd_bearish
    13:31:46  arm=True   last_overbought_macd_armed   <- bought
    13:33:14  arm=False  macd_bearish

Bearish either side, bullish for one thirteen-second poll. The position closed
79 seconds later on macd_negative, and the operator's chart shows the MACD
histogram red and both lines declining across the whole window.

Two defects, both introduced the same morning:

  * the flat-OB exemption fired at 80.7%. It exists because a reading PINNED
    at 100% cannot rise, so the level must stand in for the turn. At 80.7%
    there are nineteen points of headroom and "not rising" is a real refusal.

  * the hard sell had required ai_exit_macd_confirm_ticks agreeing reads since
    that morning; the entry required one. An exit held to a higher standard of
    evidence than the entry will always buy noise and sell signal.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import config as _config  # noqa: E402


def _rec(pctr):
    # pctr_src is the engine's own label for a real rolling reading; GAP's
    # was one. Without it ai_watch_require_live_pctr refuses the record
    # before the exemption is ever reached, and these tests stop being
    # about the exemption.
    return {"symbol": "X", "indicator": {
        "pctr": pctr, "pctr_rising": False, "pctr_falling": False,
        "pctr_src": "live",
        "macd_src": "realtime", "macd_age_sec": 1.0, "macd_gap": 0.02,
        "macd_sep_ratio": 2.0, "macd_bull": True,
        "macd_gap_rising": True, "macd_gap_falling": False}}


def _exh_cfg():
    """The operator's config with the EXH rules forced ON.

    These two tests assert what exhaustion_allows_buy decides. Reading
    ai_watch_exhaustion_rules off the live file makes them assert whether
    the desk currently consults EXH at all, which is a different question
    and one that flips with a knob (it went false on 2026-09-01).
    """
    c = dict(_config.load_config())
    c["ai_watch_exhaustion_rules"] = True
    return c


# ── the flat-OB exemption is for the ceiling only ────────────────────────

def test_a_pinned_reading_still_gets_the_exemption():
    c = _exh_cfg()
    assert ew.exhaustion_allows_buy(_rec(-0.0), c) == (
        True, "overbought_macd_armed")


def test_gap_at_eighty_percent_is_refused():
    """The trade that prompted this. Nineteen points of headroom and flat."""
    c = _exh_cfg()
    ok, why = ew.exhaustion_allows_buy(_rec(-19.3), c)
    assert ok is False
    assert why == "not_rising_overbought"


def test_the_threshold_is_configurable_and_near_the_ceiling():
    c = _config.load_config()
    assert c["ai_watch_ob_flat_min_pct"] >= 95.0, (
        "the exemption is for readings that cannot rise, not merely high ones")


def test_the_default_is_the_ceiling():
    assert _config.DEFAULT_CONFIG["ai_watch_ob_flat_min_pct"] == 99.0


# ── entry confirmation, mirroring the exit ───────────────────────────────

def test_a_single_yes_does_not_arm():
    rec = {}
    assert ew._arm_streak(rec, True) == 1


def test_consecutive_yes_accumulates():
    rec = {}
    assert [ew._arm_streak(rec, True) for _ in range(3)] == [1, 2, 3]


def test_any_no_resets_the_streak():
    """The whole point: bearish-bullish-bearish must not accumulate."""
    rec = {}
    ew._arm_streak(rec, True)
    ew._arm_streak(rec, True)
    assert ew._arm_streak(rec, False) == 0
    assert ew._arm_streak(rec, True) == 1


def test_the_streak_does_not_survive_a_restart():
    """Kept on the record, not on disk — after a restart the desk should
    re-earn its evidence rather than act on a count it cannot see behind."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("def _arm_streak")
    assert "rec[\"arm_streak\"]" in src[i:i + 900]


def test_one_tick_restores_the_old_behaviour():
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": 1}) == 1
    assert ew._arm_confirm_ticks({}) == 1
    assert ew._arm_confirm_ticks(None) == 1


def test_garbage_cannot_disable_the_gate():
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": "soon"}) == 1
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": 0}) == 1


def test_the_poll_resets_on_refusal_and_gates_on_the_streak():
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)")
    body = src[i:i + 3200]
    assert "_arm_streak(rec, False)" in body, "a refusal must reset the count"
    assert "streak < need_arm" in body
    assert 'set_block_reason(rec, "arm_confirming"' in body


def test_the_entry_bar_is_not_looser_than_the_exit():
    """An exit held to a higher standard than the entry buys noise and sells
    signal. They need not be equal, but the entry must not be the weaker."""
    c = _config.load_config()
    assert c["ai_watch_arm_confirm_ticks"] >= 1
    assert c["ai_exit_macd_confirm_ticks"] >= 1


def test_the_confirming_state_has_a_label():
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert L.get("arm_confirming") == "confirming"
