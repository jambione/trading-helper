"""T3.1 — price sparklines: shape, refusals, and fixed-width layout."""
import os
import sys

from conftest import column_cells  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    Feed,
    momentum_columns,
    momentum_table,
)
from spark import BLOCKS, FLAT, direction, sparkline  # noqa: E402
from symbol_history import SymbolHistory  # noqa: E402

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False, "focus_age_enabled": False,
       "setup_distance_enabled": False, "mention_trend_enabled": False,
       "rvol_column_enabled": False}


def _heights(s: str) -> list[int]:
    return [BLOCKS.index(c) for c in s]


# ── shape ────────────────────────────────────────────────────────────────────

def test_rising_series_renders_non_decreasing_heights():
    s = sparkline([1, 2, 3, 4, 5, 6, 7, 8], min_samples=2)
    h = _heights(s)
    assert h == sorted(h)
    assert h[0] == 0 and h[-1] == len(BLOCKS) - 1


def test_falling_series_renders_non_increasing_heights():
    s = sparkline([8, 7, 6, 5, 4, 3, 2, 1], min_samples=2)
    h = _heights(s)
    assert h == sorted(h, reverse=True)


def test_endpoints_pin_to_floor_and_ceiling():
    s = sparkline([5, 1, 9, 3], min_samples=2)
    assert min(_heights(s)) == 0
    assert max(_heights(s)) == len(BLOCKS) - 1


def test_a_spike_survives_into_the_picture():
    """Downsampling takes the last `width` samples; it must not average a
    spike away."""
    s = sparkline([1, 1, 1, 9, 1, 1, 1], min_samples=2)
    assert max(_heights(s)) == len(BLOCKS) - 1
    assert _heights(s)[3] == len(BLOCKS) - 1


def test_scaling_is_per_call_not_global():
    """Shape only — magnitude belongs to Chg%. Two windows of very different
    absolute range produce the same picture."""
    small = sparkline([10.00, 10.05, 10.10], min_samples=2, flat_pct=0.0)
    big = sparkline([10.0, 15.0, 20.0], min_samples=2, flat_pct=0.0)
    assert small == big


def test_one_sample_per_output_char():
    for n in (2, 5, 13, 20):
        assert len(sparkline(list(range(n)), width=20, min_samples=2)) == n


# ── flat handling ────────────────────────────────────────────────────────────

def test_constant_series_renders_uniform_mid_blocks_without_raising():
    s = sparkline([4, 4, 4, 4], min_samples=2)
    assert s == FLAT * 4
    assert len(set(s)) == 1


def test_a_range_that_is_only_noise_renders_flat():
    """Per-row scaling fills full height whatever the range, so a stock
    ticking 10.00/10.01 would otherwise draw the same violent zigzag as one
    moving 30%. 0.1% of 10.00 is 0.01, so this must read flat."""
    s = sparkline([10.00, 10.01, 10.00, 10.01, 10.00],
                  min_samples=2, flat_pct=0.1)
    assert s == FLAT * 5


def test_a_real_move_still_draws_shape():
    s = sparkline([10.00, 10.50, 11.00, 12.00], min_samples=2, flat_pct=0.1)
    assert s != FLAT * 4
    assert _heights(s) == sorted(_heights(s))


def test_flat_pct_zero_restores_pure_min_max_scaling():
    s = sparkline([10.00, 10.01, 10.00], min_samples=2, flat_pct=0.0)
    assert s != FLAT * 3


def test_flat_check_survives_a_zero_midpoint():
    """A window straddling zero has midpoint 0; the percentage is undefined
    and must not raise."""
    s = sparkline([-1.0, 0.0, 1.0], min_samples=2, flat_pct=0.1)
    assert len(s) == 3


# ── refusals ─────────────────────────────────────────────────────────────────

def test_empty_and_single_sample_render_nothing():
    """Absence of data must not look like absence of movement."""
    assert sparkline([]) == ""
    assert sparkline(None) == ""
    assert sparkline([5.0]) == ""


def test_below_min_samples_renders_nothing():
    assert sparkline([1, 2, 3], min_samples=5) == ""
    assert sparkline([1, 2, 3, 4, 5], min_samples=5) != ""


def test_min_samples_never_drops_below_two():
    assert sparkline([5.0], min_samples=0) == ""
    assert sparkline([5.0], min_samples=1) == ""


def test_none_and_garbage_samples_are_skipped_not_zero_filled():
    """A feed gap must not render as a plunge to the floor."""
    s = sparkline([5, None, 6, "junk", 7, float("nan"), 8], min_samples=2)
    assert len(s) == 4
    assert _heights(s) == sorted(_heights(s))


def test_gaps_can_drop_the_count_below_the_floor():
    assert sparkline([5, None, None, None, 6], min_samples=5) == ""


# ── width ────────────────────────────────────────────────────────────────────

def test_output_is_capped_at_width_keeping_the_most_recent():
    s = sparkline(list(range(100)), width=10, min_samples=2)
    assert len(s) == 10
    # The most recent samples are the rising tail, so the last block is max.
    assert _heights(s)[-1] == len(BLOCKS) - 1


def test_width_one_does_not_raise():
    assert len(sparkline([1, 2, 3], width=1, min_samples=2)) == 1


# ── direction ────────────────────────────────────────────────────────────────

def test_direction_matches_what_the_spark_depicts():
    assert direction([1, 2, 3]) == 1
    assert direction([3, 2, 1]) == -1
    assert direction([2, 5, 2]) == 0
    assert direction([2, 2, 2]) == 0


def test_direction_unknown_below_min_samples():
    assert direction([1, 9], min_samples=5) == 0
    assert direction([]) == 0


# ── rendered column ──────────────────────────────────────────────────────────

def _hist(sym, values, field="price"):
    h = SymbolHistory(maxlen=120)
    for i, v in enumerate(values):
        h.push(sym, T0 + i * 2.0, **{field: v})
    return h


def _shape_cells(rows, hist, **over):
    f = Feed(CFG)
    f.rows = rows
    t = momentum_table(f, T0, 0.5, True, cfg={**CFG, **over}, history=hist)
    return column_cells(t, "Shape")


def test_rendered_spark_is_green_when_rising():
    hist = _hist("AAAA", [1, 2, 3, 4, 5, 6])
    cell = _shape_cells([{"ticker": "AAAA"}], hist)[0]
    assert "green" in cell
    assert "red" not in cell


def test_rendered_spark_is_red_when_falling():
    hist = _hist("AAAA", [6, 5, 4, 3, 2, 1])
    assert "red" in _shape_cells([{"ticker": "AAAA"}], hist)[0]


def test_column_width_is_constant_regardless_of_history_depth():
    """The table must not jitter as symbols accumulate tape."""
    widths = set()
    for n in (0, 1, 4, 5, 12, 40, 200):
        hist = _hist("AAAA", list(range(n)))
        cell = _shape_cells([{"ticker": "AAAA"}], hist)[0]
        plain = (cell.replace("[green]", "").replace("[/green]", "")
                     .replace("[red]", "").replace("[/red]", "")
                     .replace("[dim]", "").replace("[/dim]", ""))
        widths.add(len(plain))
    assert widths == {int(CFG["spark_width"])}, widths


def test_short_history_renders_blank_not_a_shape():
    hist = _hist("AAAA", [1, 5, 2])          # 3 samples, floor is 5
    cell = _shape_cells([{"ticker": "AAAA"}], hist)[0]
    assert not any(b in cell for b in BLOCKS)


def test_no_history_object_renders_blank_padding():
    f = Feed(CFG)
    f.rows = [{"ticker": "AAAA"}]
    t = momentum_table(f, T0, 0.5, True, cfg=CFG, history=None)
    assert column_cells(t, "Shape")[0] == " " * int(CFG["spark_width"])



# ── column placement ─────────────────────────────────────────────────────────

def test_flag_off_removes_the_column():
    headers = [h for h, _, _ in momentum_columns(
        {**CFG, "spark_enabled": False})]
    assert "Shape" not in headers


def test_shape_sits_after_rvol_when_both_are_on():
    headers = [h for h, _, _ in momentum_columns(
        {**CFG, "rvol_column_enabled": True, "spark_enabled": True})]
    assert headers.index("Shape") == headers.index("RVOL") + 1
    assert headers.index("RVOL") == headers.index("Chg%") + 1


def test_shape_falls_back_to_chg_anchor_when_rvol_is_off():
    headers = [h for h, _, _ in momentum_columns(
        {**CFG, "rvol_column_enabled": False, "spark_enabled": True})]
    assert headers.index("Shape") == headers.index("Chg%") + 1


def test_historical_columns_keep_their_order_with_both_optionals_on():
    from momentum_signal import MOMENTUM_COLUMNS
    headers = [h for h, _, _ in momentum_columns(
        {**CFG, "rvol_column_enabled": True, "spark_enabled": True})]
    base = [h for h, _, _ in MOMENTUM_COLUMNS]
    assert [h for h in headers if h in base] == base
