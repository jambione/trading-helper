"""A replay has to rebuild MACD direction, because no engine publishes it.

WHAT BROKE, AND HOW IT LOOKED LIKE A RESULT

On 2026-09-05 the declared arm-gate A/B
(tools/rstop_search_arm_exh_rsi_macd_veto_ab.json) scored four cells over
2026-09-01..04 and returned:

    do_not_promote  n=155  require_macd=False  (both narrowing settings)
    hypothesis      n=0    require_macd=True   (both narrowing settings)

n=0 reads as "the MACD stack refuses every trade". It was not: the sim
could not answer the question at all, for two independent reasons.

  1. macd_gap_rising / macd_gap_falling come off the signal engine
     (strategy_three_indicator.evaluate). live_macd publishes the SIZE of
     the gap and never its direction, and a replay has no engine — so
     inside the full stack macd_narrowing_blocks_buy, which fails closed on
     unknown direction by design, answered macd_gap_dir_unknown on every
     bar.

  2. The live config carries ai_watch_macd_max_age_sec=60. live_macd stamps
     macd_src=realtime with no age, and a positive ceiling refuses a
     missing age as macd_src_unknown. A replay has no wall clock to be late
     against.

Both are the class of bug replay_cfg already existed for (ai_watch_cm_rsi_local
=False makes should_arm_buy answer no_rsi_data on every bar of a sim). The
tests below fail on the pre-fix code with exactly those two reasons.
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import ai_entry_watch as ew  # noqa: E402
import sim_rstop_path as sim  # noqa: E402


def _bars(n: int, start: float, step: float) -> list[tuple]:
    """n closed 1m bars walking by *step* — (ts, o, h, l, c)."""
    now = time.time()
    out = []
    for i in range(n):
        px = start + i * step
        out.append((now - (n - i) * 60.0, px, px + 0.01, px - 0.01, px))
    return out


def _prime(symbol: str, bars: list[tuple]) -> float:
    sim.prime_ohlc(symbol, bars, time.time())
    return bars[-1][4]


# ── replay_cfg ───────────────────────────────────────────────────────────

def test_replay_cfg_drops_the_wall_clock_freshness_ceiling():
    out = sim.replay_cfg({"ai_watch_macd_max_age_sec": 60.0})
    assert out["ai_watch_macd_max_age_sec"] == 0.0
    assert out["ai_watch_cm_rsi_local"] is True


def test_replay_cfg_is_still_idempotent():
    once = sim.replay_cfg({"ai_watch_macd_max_age_sec": 60.0})
    assert sim.replay_cfg(once) is once


def test_the_rsi_guard_alone_no_longer_short_circuits_the_macd_fix():
    """The bug the old guard would have caused.

    replay_cfg returned early on ai_watch_cm_rsi_local alone. A config that
    already had the RSI fix applied — every caller that reuses one — would
    have skipped the age ceiling forever.
    """
    cfg = {"ai_watch_cm_rsi_local": True, "ai_watch_macd_max_age_sec": 60.0}
    assert sim.replay_cfg(cfg)["ai_watch_macd_max_age_sec"] == 0.0


# ── direction off the bars ───────────────────────────────────────────────

def test_direction_is_filled_from_the_primed_bars():
    # A base then a break, not a straight ramp: on a constant-slope series
    # the histogram decays toward zero, so "price up" and "gap opening" are
    # different statements — which is the whole reason direction is a
    # separate reading from size.
    px = _prime("RISE", _bars(60, 10.0, 0.0) + _bars(10, 10.08, 0.08))
    rec = sim.make_rec("RISE", px, {}, source="momentum")
    ew.apply_live_exhaustion(rec, px, {}, time.time())
    assert sim.apply_macd_direction(rec, "RISE", px, {}, time.time()) is True
    ind = rec["indicator"]
    assert ind["macd_gap_rising"] is True
    assert ind["macd_gap_falling"] is False
    assert ind["macd_gap_prev"] is not None


def test_a_rolling_over_series_reads_as_falling():
    # Up hard, then a turn: the histogram is still positive but shrinking,
    # which is the reading macd_gap_narrowing exists to catch.
    bars = _bars(70, 10.0, 0.05) + _bars(12, 13.5, -0.04)
    px = _prime("TURN", bars)
    rec = sim.make_rec("TURN", px, {}, source="momentum")
    ew.apply_live_exhaustion(rec, px, {}, time.time())
    sim.apply_macd_direction(rec, "TURN", px, {}, time.time())
    assert rec["indicator"]["macd_gap_falling"] is True
    assert rec["indicator"]["macd_gap_rising"] is False


def test_too_few_bars_leaves_direction_unset_rather_than_guessing():
    px = _prime("SHORT", _bars(6, 10.0, 0.02))
    rec = sim.make_rec("SHORT", px, {}, source="momentum")
    assert sim.apply_macd_direction(rec, "SHORT", px, {}, time.time()) is False
    assert "macd_gap_rising" not in rec["indicator"]


# ── the verdict that was n=0 ─────────────────────────────────────────────

def _full_stack_reason(symbol: str, bars: list[tuple]) -> str:
    px = _prime(symbol, bars)
    cfg = {
        "ai_watch_arm_require_macd": True,
        "ai_watch_macd_block_narrowing": True,
        "ai_watch_require_realtime_macd": True,
        "ai_watch_macd_max_age_sec": 60.0,   # the live value
        "ai_watch_arm_require_indicators": False,
        "ai_watch_require_exhaustion_data": False,
        "ai_watch_require_db_zone": False,
    }
    rec = sim.make_rec(symbol, px, cfg, source="momentum")
    _ok, why = sim.try_arm(rec, px, cfg, time.time())
    return why


def test_the_full_stack_no_longer_answers_dir_unknown_on_every_bar():
    why = _full_stack_reason("RISE2", _bars(80, 10.0, 0.02))
    assert why != "macd_gap_dir_unknown"


def test_the_full_stack_no_longer_answers_src_unknown_on_every_bar():
    why = _full_stack_reason("RISE3", _bars(80, 10.0, 0.02))
    assert why != "macd_src_unknown"


def test_a_rolling_over_series_can_still_be_refused_for_a_real_reason():
    """The fix must not make the gate toothless — only answerable.

    A turned-over series should reach a MACD verdict about the trade, not a
    verdict about the simulator's instrumentation.
    """
    bars = _bars(70, 10.0, 0.05) + _bars(12, 13.5, -0.04)
    why = _full_stack_reason("TURN2", bars)
    assert why not in ("macd_gap_dir_unknown", "macd_src_unknown",
                       "no_macd_data")
