"""T1.2 — distance-to-trigger: monotonicity, boundaries, None paths, sort."""
import os
import sys
from itertools import pairwise

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    Feed,
    momentum_table,
    row_rank,
    setup_distance,
    setup_shortfall,
)

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False}


def _row(**sp):
    return {"signal_proximity": sp}


class _NullAlerter:
    def fire(self, *a, **k):
        pass


# ── firing rows ──────────────────────────────────────────────────────────────

def test_a_firing_row_scores_exactly_zero():
    d = setup_distance(_row(cm_rsi=22.0, pctr=-91.0, pctr_slow=-88.0,
                            pctr_deep_os=True))
    assert d == 0.0


def test_firing_row_stays_zero_despite_low_proximity_pct():
    """proximity_pct is the THREE-indicator completion and includes MACD,
    which FOCUS does not. A firing setup with no MACD cross reports well
    under 100 and must not be pushed off zero by the blend."""
    d = setup_distance(_row(cm_rsi=22.0, pctr=-91.0, pctr_slow=-88.0,
                            pctr_deep_os=True, proximity_pct=67))
    assert d == 0.0


def test_firing_row_sorts_below_every_near_miss():
    firing = setup_distance(_row(cm_rsi=22.0, pctr=-91.0, pctr_slow=-88.0,
                                 pctr_deep_os=True, proximity_pct=67))
    near = setup_distance(_row(cm_rsi=35.1, pctr=-99.0, pctr_slow=-98.0,
                               pctr_falling=True, pctr_slow_falling=True,
                               proximity_pct=99))
    assert firing < near


def test_in_band_but_flag_withheld_is_not_zero():
    """_pctr_leg_ok trusts the engine's pctr_deep_os flag over the raw band.
    A row sitting inside the band whose flag is False is NOT firing, so it
    must score above zero even though every positional gap is zero."""
    row = _row(cm_rsi=20.0, pctr=-95.0, pctr_slow=-90.0, pctr_deep_os=False)
    d = setup_distance(row)
    assert d is not None
    assert d > 0.0


# ── monotonicity ─────────────────────────────────────────────────────────────

def test_score_decreases_as_rsi_falls_toward_the_threshold():
    """The %R leg is held constant so only the RSI gap moves."""
    scores = [
        setup_distance(_row(cm_rsi=rsi, pctr=-78.0, pctr_slow=-72.0))
        for rsi in (90.0, 70.0, 55.0, 45.0, 38.0, 36.0)
    ]
    assert all(a > b for a, b in pairwise(scores)), scores


def test_score_decreases_as_pctr_approaches_the_band():
    scores = [
        setup_distance(_row(cm_rsi=20.0, pctr=v, pctr_slow=v))
        for v in (-10.0, -30.0, -50.0, -65.0, -74.0)
    ]
    assert all(a > b for a, b in pairwise(scores)), scores


def test_distance_is_continuous_across_the_rsi_boundary():
    """The RSI gap is genuinely zero at rsi_max, so the score does not jump.

    Both rows still fail the %R leg, so neither reaches 0.0 — the score
    measures how much price/indicator movement is left, and at the threshold
    that is nothing.
    """
    at = setup_distance(_row(cm_rsi=35.0, pctr=-78.0, pctr_slow=-72.0))
    below = setup_distance(_row(cm_rsi=34.99, pctr=-78.0, pctr_slow=-72.0))
    assert at == below
    assert at > 0.0


def test_the_trigger_flips_at_exactly_rsi_max():
    """cm_rsi < rsi_max is the band. With the %R leg already satisfied,
    34.99 fires (0.0) and 35.0 does not."""
    fires = setup_distance(_row(cm_rsi=34.99, pctr=-91.0, pctr_slow=-88.0,
                                pctr_deep_os=True))
    holds = setup_distance(_row(cm_rsi=35.0, pctr=-91.0, pctr_slow=-88.0,
                                pctr_deep_os=True))
    assert fires == 0.0
    assert holds > 0.0


def test_score_is_bounded_to_the_unit_interval():
    for row in (_row(cm_rsi=100.0, pctr=0.0, pctr_slow=0.0),
                _row(cm_rsi=0.0, pctr=-100.0, pctr_slow=-100.0),
                _row(cm_rsi=99.0, pctr=0.0, pctr_slow=0.0,
                     proximity_pct=0)):
        d = setup_distance(row)
        assert d is not None and 0.0 <= d <= 1.0


# ── None paths ───────────────────────────────────────────────────────────────

def test_untracked_row_returns_none():
    assert setup_distance({}) is None
    assert setup_distance({"signal_proximity": {}}) is None


def test_pending_row_without_cm_rsi_returns_none():
    assert setup_distance(_row(status="watching")) is None


def test_missing_pctr_pair_is_treated_as_far_not_satisfied():
    """No %R published yet must not read as "the %R leg is fine"."""
    d = setup_distance(_row(cm_rsi=20.0))
    assert d is not None and d > 0.3


# ── proximity blend ──────────────────────────────────────────────────────────

def test_proximity_weight_zero_ignores_the_engine_number():
    row = _row(cm_rsi=38.0, pctr=-78.0, pctr_slow=-72.0, proximity_pct=0)
    bare = setup_distance(_row(cm_rsi=38.0, pctr=-78.0, pctr_slow=-72.0))
    assert setup_distance(row, proximity_weight=0.0) == bare


def test_higher_proximity_pct_lowers_the_distance():
    lo = setup_distance(_row(cm_rsi=38.0, pctr=-78.0, pctr_slow=-72.0,
                             proximity_pct=10))
    hi = setup_distance(_row(cm_rsi=38.0, pctr=-78.0, pctr_slow=-72.0,
                             proximity_pct=95))
    assert hi < lo


# ── shortfall text ───────────────────────────────────────────────────────────

def test_shortfall_names_only_the_failing_legs():
    both = setup_shortfall(_row(cm_rsi=38.0, pctr=-78.0, pctr_slow=-72.0))
    assert both == "rsi 38→35  %R -72→-75"


def test_shortfall_omits_a_satisfied_rsi_leg():
    s = setup_shortfall(_row(cm_rsi=20.0, pctr=-78.0, pctr_slow=-72.0))
    assert s == "%R -72→-75"


def test_shortfall_reports_the_worse_of_the_two_pctr_lines():
    s = setup_shortfall(_row(cm_rsi=20.0, pctr=-50.0, pctr_slow=-72.0))
    assert "-50" in s


def test_shortfall_empty_when_firing():
    assert setup_shortfall(_row(cm_rsi=22.0, pctr=-91.0, pctr_slow=-88.0,
                                pctr_deep_os=True)) == ""


# ── cell rendering ───────────────────────────────────────────────────────────

def _setup_cells(rows, **over):
    f = Feed(CFG)
    f.rows = rows
    cfg = {**CFG, "focus_age_enabled": False, **over}
    t = momentum_table(f, T0, 0.5, True, cfg=cfg)
    return list(list(t.columns)[6].cells)


def test_near_miss_renders_the_shortfall():
    rows = [{"ticker": "BBBB", "signal_proximity": {
        "cm_rsi": 38.0, "pctr": -78.0, "pctr_slow": -72.0,
        "proximity_pct": 67}}]
    cell = _setup_cells(rows)[0]
    assert "NEAR" in cell
    assert "rsi 38→35" in cell
    assert "%R -72→-75" in cell


def test_far_row_keeps_todays_dim_readout():
    rows = [{"ticker": "BBBB", "signal_proximity": {
        "cm_rsi": 88.0, "pctr": -10.0, "pctr_slow": -5.0}}]
    cell = _setup_cells(rows)[0]
    assert "NEAR" not in cell
    assert cell == "[dim]88·-10/-5[/dim]"


def test_firing_row_still_renders_focus_not_near():
    rows = [{"ticker": "AAAA", "signal_proximity": {
        "cm_rsi": 22.0, "pctr": -91.0, "pctr_slow": -88.0,
        "pctr_deep_os": True}}]
    cell = _setup_cells(rows)[0]
    assert "FOCUS" in cell
    assert "NEAR" not in cell


def test_missing_signal_proximity_renders_a_dash_as_today():
    cell = _setup_cells([{"ticker": "CCCC"}])[0]
    assert cell == "[dim]—[/dim]"


def test_flag_off_restores_todays_dim_readout():
    rows = [{"ticker": "BBBB", "signal_proximity": {
        "cm_rsi": 38.0, "pctr": -78.0, "pctr_slow": -72.0,
        "proximity_pct": 67}}]
    off = _setup_cells(rows, setup_distance_enabled=False)[0]
    assert off == "[dim]38·-78/-72[/dim]"
    assert "NEAR" in _setup_cells(rows, setup_distance_enabled=True)[0]


def test_near_threshold_gates_the_badge():
    rows = [{"ticker": "BBBB", "signal_proximity": {
        "cm_rsi": 38.0, "pctr": -78.0, "pctr_slow": -72.0,
        "proximity_pct": 67}}]
    assert "NEAR" not in _setup_cells(rows, setup_near_threshold=0.0)[0]
    assert "NEAR" in _setup_cells(rows, setup_near_threshold=1.0)[0]


# ── ranking integration ──────────────────────────────────────────────────────

def test_sort_disabled_keeps_the_phase_0_key():
    cfg = {**CFG, "setup_sort_enabled": False}
    row = _row(cm_rsi=22.0, pctr_deep_os=True)
    assert row_rank(row, T0, T0, 3, cfg) == (-T0, 3)


def test_sort_enabled_inserts_distance_beneath_freshness():
    cfg = {**CFG, "setup_sort_enabled": True}
    firing = row_rank(_row(cm_rsi=22.0, pctr=-91.0, pctr_slow=-88.0,
                           pctr_deep_os=True), T0, T0, 9, cfg)
    far = row_rank(_row(cm_rsi=88.0, pctr=-10.0, pctr_slow=-5.0),
                   T0, T0, 0, cfg)
    assert len(firing) == 3
    assert firing < far, "distance must outrank server order"


def test_freshness_still_dominates_setup_distance():
    cfg = {**CFG, "setup_sort_enabled": True}
    fresh_far = row_rank(_row(cm_rsi=88.0, pctr=-10.0, pctr_slow=-5.0),
                         T0, T0, 0, cfg)
    stale_firing = row_rank(_row(cm_rsi=22.0, pctr_deep_os=True),
                            T0 - 60.0, T0, 0, cfg)
    assert fresh_far < stale_firing


def test_untracked_rows_park_with_the_far_rows():
    cfg = {**CFG, "setup_sort_enabled": True}
    untracked = row_rank({}, T0, T0, 0, cfg)
    near = row_rank(_row(cm_rsi=35.1, pctr=-99.0, pctr_slow=-98.0,
                         pctr_falling=True, pctr_slow_falling=True),
                    T0, T0, 9, cfg)
    assert near < untracked


def test_ingest_ordering_unchanged_when_sort_disabled():
    rows = [
        {"ticker": "AAAA", "signal_proximity": {"cm_rsi": 88.0}},
        {"ticker": "BBBB", "signal_proximity": {"cm_rsi": 22.0,
                                                "pctr_deep_os": True}},
        {"ticker": "CCCC"},
    ]
    f = Feed({**CFG, "setup_sort_enabled": False})
    f.ingest({"tickers": [dict(r) for r in rows]}, T0, _NullAlerter(),
             {**CFG, "setup_sort_enabled": False})
    assert [r["ticker"] for r in f.rows] == ["AAAA", "BBBB", "CCCC"]


def test_ingest_ordering_promotes_firing_rows_when_sort_enabled():
    rows = [
        {"ticker": "AAAA", "signal_proximity": {"cm_rsi": 88.0,
                                                "pctr": -10.0,
                                                "pctr_slow": -5.0}},
        {"ticker": "BBBB", "signal_proximity": {"cm_rsi": 22.0,
                                                "pctr": -91.0,
                                                "pctr_slow": -88.0,
                                                "pctr_deep_os": True}},
        {"ticker": "CCCC"},
    ]
    cfg = {**CFG, "setup_sort_enabled": True}
    f = Feed(cfg)
    f.ingest({"tickers": [dict(r) for r in rows]}, T0, _NullAlerter(), cfg)
    assert [r["ticker"] for r in f.rows] == ["BBBB", "AAAA", "CCCC"]


def test_sorted_order_is_stable_across_repeated_polls():
    rows = [
        {"ticker": "AAAA", "signal_proximity": {"cm_rsi": 40.0,
                                                "pctr": -70.0,
                                                "pctr_slow": -70.0}},
        {"ticker": "BBBB", "signal_proximity": {"cm_rsi": 40.0,
                                                "pctr": -70.0,
                                                "pctr_slow": -70.0}},
        {"ticker": "CCCC", "signal_proximity": {"cm_rsi": 40.0,
                                                "pctr": -70.0,
                                                "pctr_slow": -70.0}},
    ]
    cfg = {**CFG, "setup_sort_enabled": True}
    f = Feed(cfg)
    seen = []
    for i in range(5):
        f.ingest({"tickers": [dict(r) for r in rows]}, T0 + i * 2.0,
                 _NullAlerter(), cfg)
        seen.append([r["ticker"] for r in f.rows])
    assert all(s == seen[0] for s in seen), seen


def test_a_raising_distance_does_not_break_the_sort():
    """row_rank must survive a malformed row rather than kill the loop."""
    cfg = {**CFG, "setup_sort_enabled": True}
    key = row_rank({"signal_proximity": {"cm_rsi": "junk"}}, T0, T0, 1, cfg)
    assert len(key) == 3
