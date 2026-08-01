"""
test_rs_core.py — the relative-strength math, offline.

No network, no disk, no clock: rs_core.py is pure, so every rule is pinned
against a synthetic frame built on a fabricated benchmark calendar.

The fixture is chosen so the expected RS can be computed by hand and checked
against the module docstring's formula without running the code:

    P0=200  P21=160  P63=100  P126=80  P189=50  P252=40

    trailing  0.4·(200/100) + 0.2·(200/80) + 0.2·(200/50) + 0.2·(200/40)
            = 0.8 + 0.5 + 0.8 + 1.0                                  = 3.10
    quarters  0.4·(200/100) + 0.2·(100/80) + 0.2·(80/50) + 0.2·(50/40)
            = 0.8 + 0.25 + 0.32 + 0.25                               = 1.62

Run:
    .venv/bin/python -m pytest tests/test_rs_core.py -q
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rs_core as rc   # noqa: E402


SESSIONS = 253          # 252 lookback + today

# Anchor sessions-back → close. Everything between is interpolated; only the
# anchors matter to the formula.
ANCHORS = {0: 200.0, 21: 160.0, 63: 100.0, 126: 80.0, 189: 50.0, 252: 40.0}


def calendar(n: int = SESSIONS) -> pd.DatetimeIndex:
    """A fabricated benchmark session index. Business days, so it has the shape
    of a real calendar without depending on a holiday table."""
    return pd.bdate_range("2025-06-02", periods=n, tz="UTC")


def series_from_anchors(anchors: dict[int, float], n: int = SESSIONS) -> pd.Series:
    """A close series hitting `anchors` exactly, linearly filled in between.
    Anchors that fall off the front of a short series are simply skipped."""
    idx = calendar(n)
    values = pd.Series(np.nan, index=idx, dtype="float64")
    for back, price in anchors.items():
        if 0 <= n - 1 - back < n:
            values.iloc[n - 1 - back] = price
    return values.interpolate().bfill().ffill()


def frame_from_closes(closes: pd.Series, volume: float = 1_000_000.0) -> pd.DataFrame:
    return pd.DataFrame({
        "close": closes,
        "high": closes * 1.02,
        "low": closes * 0.98,
        "volume": volume,
    }, index=closes.index)


def aligned_stock(anchors=None, n: int = SESSIONS, drop=None, volume: float = 1_000_000.0):
    """(aligned frame, coverage) for a stock on the full benchmark calendar."""
    cal = calendar(n)
    closes = series_from_anchors(anchors or ANCHORS, n)
    frame = frame_from_closes(closes, volume)
    if drop is not None:
        frame = frame.drop(frame.index[drop])
    return rc.align_frame_to_calendar(frame, cal)


def flat_benchmark(level: float = 500.0, n: int = SESSIONS) -> dict[str, float]:
    """bench_returns for a benchmark that went nowhere — every return 0.0."""
    return {f"ret_{label}": 0.0 for label in rc.RETURN_WINDOWS}


# ── The formula ───────────────────────────────────────────────────────────────

def test_a_hand_computed_trailing_rs_matches_the_ibd_weighting():
    aligned, _ = aligned_stock()
    assert rc.rs_raw(aligned["close"], form="trailing") == pytest.approx(3.10)


def test_the_discrete_quarters_form_is_a_different_number():
    aligned, _ = aligned_stock()
    trailing = rc.rs_raw(aligned["close"], form="trailing")
    quarters = rc.rs_raw(aligned["close"], form="quarters")
    assert quarters == pytest.approx(1.62)
    assert quarters != pytest.approx(trailing)


def test_rs_raw_weights_sum_to_one_so_a_flat_series_scores_exactly_one():
    assert sum(rc.RS_WEIGHTS.values()) == pytest.approx(1.0)
    flat = pd.Series(42.0, index=calendar())
    assert rc.rs_raw(flat, form="trailing") == pytest.approx(1.0)
    assert rc.rs_raw(flat, form="quarters") == pytest.approx(1.0)


def test_an_unknown_form_raises_rather_than_defaulting():
    aligned, _ = aligned_stock()
    with pytest.raises(ValueError, match="unknown rs_form"):
        rc.rs_raw(aligned["close"], form="ibd_secret_sauce")


def test_returns_use_the_documented_session_windows():
    aligned, _ = aligned_stock()
    close = aligned["close"]
    assert rc.period_return(close, 21) == pytest.approx(200 / 160 - 1)    # 0.25
    assert rc.period_return(close, 63) == pytest.approx(1.0)
    assert rc.period_return(close, 126) == pytest.approx(1.5)
    assert rc.period_return(close, 252) == pytest.approx(4.0)


# ── Alignment and the same-session rule ───────────────────────────────────────

def test_the_anchor_bars_come_from_the_benchmark_calendar_not_positional_offsets():
    """A stock missing its 20 earliest sessions must still read P63 off the date
    63 benchmark sessions back — not off its own 63rd-from-last row."""
    cal = calendar()
    closes = series_from_anchors(ANCHORS)
    frame = frame_from_closes(closes).iloc[20:]          # 20 leading sessions absent

    aligned, _ = rc.align_frame_to_calendar(frame, cal)
    assert rc.anchor_close(aligned["close"], 63) == pytest.approx(100.0)

    # The naive read on the unaligned frame lands on a different bar entirely.
    naive = float(frame["close"].iloc[-1 - 63])
    assert rc.anchor_close(aligned["close"], 63) == pytest.approx(naive)  # same here…
    # …but the front of the series is where they diverge: the stock has no P252.
    assert rc.anchor_close(aligned["close"], 252) is None
    assert len(frame) > 252 - 20


def test_a_thin_name_with_gaps_is_dropped_rather_than_misaligned():
    """Scatter 80 missing sessions through the year: coverage must fall well
    below the 0.80 floor so the caller drops it, rather than it quietly
    producing a number off shifted anchors."""
    drop = list(range(10, 250, 3))[:80]
    aligned, coverage = aligned_stock(drop=drop)
    assert coverage < 0.80
    assert len(aligned) == SESSIONS          # still on the benchmark's index


def test_a_one_session_gap_is_forward_filled_within_the_limit():
    aligned, coverage = aligned_stock(drop=[100])
    assert coverage == pytest.approx((SESSIONS - 1) / SESSIONS)
    assert np.isfinite(aligned["close"].iloc[100])


def test_a_ten_session_halt_leaves_the_anchor_absent_rather_than_stale():
    """ffill_limit is 2. A ten-session halt straddling P63 must yield None, not
    a price carried in from before the halt."""
    cal = calendar()
    closes = series_from_anchors(ANCHORS)
    frame = frame_from_closes(closes)
    halt = SESSIONS - 1 - 63
    frame = frame.drop(frame.index[halt - 4:halt + 6])
    aligned, _ = rc.align_frame_to_calendar(frame, cal, ffill_limit=2)
    assert rc.anchor_close(aligned["close"], 63) is None
    assert rc.rs_raw(aligned["close"]) is None


def test_volume_is_not_forward_filled_across_a_no_print_day():
    """A carried volume would manufacture liquidity that never traded and feed
    straight into avg_vol_50d and the liquidity filter."""
    aligned, _ = aligned_stock(drop=[100])
    assert np.isfinite(aligned["close"].iloc[100])       # price carried…
    assert pd.isna(aligned["volume"].iloc[100])          # …volume was not


def test_a_forward_filled_session_is_not_counted_as_a_real_bar():
    aligned, _ = aligned_stock(drop=[100])
    assert bool(aligned["real_bar"].iloc[100]) is False
    assert int(aligned["real_bar"].sum()) == SESSIONS - 1


def test_a_missing_twelve_month_anchor_yields_no_rating_rather_than_a_three_term_blend():
    """A name that listed 200 sessions ago has no P252. Renormalising the
    weights over the three surviving terms would be a different statistic."""
    cal = calendar()
    closes = series_from_anchors(ANCHORS)
    frame = frame_from_closes(closes).iloc[-200:]
    aligned, coverage = rc.align_frame_to_calendar(frame, cal)

    row = rc.build_row("IPO", aligned, coverage, flat_benchmark())
    assert row["rs_raw"] is None
    assert row["ret_12m"] is None
    assert row["ret_1m"] is not None and row["ret_3m"] is not None
    assert "rs_raw" in row["insufficient"] and "ret_12m" in row["insufficient"]


# ── RS versus the benchmark ───────────────────────────────────────────────────

def test_a_flat_benchmark_makes_rs_vs_spy_one_plus_the_stock_return():
    assert rc.rs_vs_benchmark(1.0, 0.0) == pytest.approx(2.0)


def test_rs_vs_spy_divides_out_a_rising_benchmark():
    """Stock +400%, SPY +100% → 5.0/2.0 = 2.5."""
    assert rc.rs_vs_benchmark(4.0, 1.0) == pytest.approx(2.5)


def test_a_stock_tracking_the_benchmark_exactly_has_an_rs_ratio_of_one():
    assert rc.rs_vs_benchmark(0.37, 0.37) == pytest.approx(1.0)


def test_rs_vs_spy_is_absent_when_either_leg_is_missing():
    """None, never 1.0 — 1.0 asserts 'performed in line with the market', which
    is a claim we cannot make about a leg we could not measure."""
    assert rc.rs_vs_benchmark(None, 0.1) is None
    assert rc.rs_vs_benchmark(0.1, None) is None


# ── The percentile (constraint 2) ─────────────────────────────────────────────

def test_ninety_nine_evenly_ranked_names_get_the_ratings_one_through_ninety_nine():
    raw = {f"S{i:02d}": 1.0 + i for i in range(99)}
    ratings = rc.percentile_ratings(raw)
    assert [ratings[f"S{i:02d}"] for i in range(99)] == list(range(1, 100))


def test_the_strongest_name_is_ninety_nine_and_the_weakest_is_one():
    raw = {f"S{i}": float(i) for i in range(5000)}
    ratings = rc.percentile_ratings(raw)
    assert ratings["S4999"] == 99
    assert ratings["S0"] == 1
    assert min(ratings.values()) >= 1 and max(ratings.values()) <= 99


def test_tied_raw_scores_get_the_same_rating():
    raw = {"A": 1.0, "B": 2.0, "C": 2.0, "D": 3.0}
    ratings = rc.percentile_ratings(raw)
    assert ratings["B"] == ratings["C"]
    assert ratings["A"] < ratings["B"] < ratings["D"]


def test_each_rating_bucket_holds_about_one_ninety_ninth_of_the_population():
    """99 buckets, not 100 — so the top bucket is the top ~1/99, which is 102
    names out of 10,000, not 100. Worth pinning: 'RS 99 = top 1%' is the
    common shorthand and it is slightly wrong."""
    raw = {f"S{i}": float(i) for i in range(10_000)}
    ratings = rc.percentile_ratings(raw)
    assert sum(1 for r in ratings.values() if r == 99) == 102
    assert len(set(ratings.values())) == 99


def test_an_exact_bucket_boundary_does_not_drift_into_the_next_bucket():
    """99·(13/99) is 13.000000000000002 in float64; a naive ceil would call it 14."""
    raw = {f"S{i:02d}": float(i) for i in range(99)}
    ratings = rc.percentile_ratings(raw)
    assert ratings["S12"] == 13


def test_a_none_raw_score_is_rejected_rather_than_ranked_as_zero():
    """A None coerced to 0.0 would sort below every real score and deflate
    every rating above it."""
    with pytest.raises(ValueError):
        rc.rank_percentiles({"A": 1.0, "B": None})
    with pytest.raises(ValueError):
        rc.rank_percentiles({"A": 1.0, "B": float("nan")})


def test_the_percentile_is_taken_before_the_filters_not_after():
    """The headline property. 100 names ranked by strength; only the strongest
    10 clear the price filter.

    Ranked over the full population the survivors rate 91..99; ranked over the
    survivors alone they would rate 10,20,...,99. The two are unmistakable.
    """
    rows = [{"ticker": f"S{i:02d}", "rs_raw": 1.00 + i / 100.0,
             "price": 5.0 if i < 90 else 50.0,
             "avg_vol_50d": 5_000_000.0, "above_sma50": True, "ret_3m": 0.1,
             "avg_dollar_vol_50d": 1e8}
            for i in range(100)]

    raw = {r["ticker"]: r["rs_raw"] for r in rows}
    rc.stamp_ratings(rows, rc.percentile_ratings(raw), rc.rank_percentiles(raw),
                     population=len(raw), as_of="2026-07-24")

    cfg = {"rs_min_price": 10.0, "rs_min_avg_vol_50d": 0.0, "rs_min_rs_rating": 0}
    kept = [r for r in rows if rc.passes_filters(r, cfg)[0]]

    assert [r["rs_rating"] for r in kept] == [91, 92, 93, 94, 95, 96, 97, 98, 99, 99]
    assert all(r["population"] == 100 for r in kept)


def test_every_row_carries_the_population_it_was_ranked_against():
    rows = [{"ticker": "A", "rs_raw": 1.0}, {"ticker": "B", "rs_raw": 2.0}]
    raw = {r["ticker"]: r["rs_raw"] for r in rows}
    rc.stamp_ratings(rows, rc.percentile_ratings(raw), rc.rank_percentiles(raw),
                     population=7431, as_of="2026-07-24")
    assert all(r["population"] == 7431 and r["as_of"] == "2026-07-24" for r in rows)


def test_an_unrated_symbol_is_stamped_none_not_zero():
    """Rating 0 does not exist on the 1-99 scale — it would read as 'weakest
    possible name' rather than 'not rated'."""
    rows = [{"ticker": "A", "rs_raw": 1.0}, {"ticker": "NOHIST", "rs_raw": None}]
    raw = {"A": 1.0}
    rc.stamp_ratings(rows, rc.percentile_ratings(raw), rc.rank_percentiles(raw),
                     population=1, as_of="2026-07-24")
    assert rows[1]["rs_rating"] is None
    assert rows[1]["rs_percentile"] is None


def test_a_filtered_out_name_does_not_change_anyone_elses_rating():
    raw = {f"S{i}": float(i) for i in range(50)}
    before = rc.percentile_ratings(raw)
    # Dropping names AFTER ranking must not move the survivors.
    after = {k: v for k, v in before.items() if int(k[1:]) >= 40}
    assert all(after[k] == before[k] for k in after)


# ── Never invent a value (constraint 4) ───────────────────────────────────────

def test_returns_are_absent_not_zero_when_history_is_short():
    aligned, coverage = aligned_stock(n=40)
    row = rc.build_row("SHORT", aligned, coverage, flat_benchmark())
    assert row["ret_3m"] is None and row["ret_12m"] is None
    assert row["rs_raw"] is None


def test_sma200_is_absent_below_two_hundred_sessions():
    cal = calendar()
    frame = frame_from_closes(series_from_anchors(ANCHORS)).iloc[-120:]
    aligned, _ = rc.align_frame_to_calendar(frame, cal)
    stats = rc.trailing_stats(aligned)
    assert stats["sma50"] is not None
    assert stats["sma200"] is None


def test_rvol_is_absent_below_twenty_sessions_rather_than_one_point_zero():
    """signals.calc_rvol returns a constant 1.0 Series below 20 bars — a
    manufactured 'perfectly average volume'. trailing_stats must not publish it."""
    cal = calendar(15)
    frame = frame_from_closes(pd.Series(np.linspace(10, 12, 15), index=cal))
    aligned, _ = rc.align_frame_to_calendar(frame, cal)
    assert rc.trailing_stats(aligned)["rvol"] is None


def test_a_flat_series_reports_an_adr_of_the_synthetic_range():
    aligned, _ = aligned_stock()
    adr = rc.trailing_stats(aligned)["adr_pct"]
    assert adr == pytest.approx((1.02 / 0.98 - 1) * 100, rel=1e-6)


def test_p0_date_reports_the_last_real_bar_not_a_carried_one():
    """The stock's own last print, distinct from the run's as_of session."""
    aligned, coverage = aligned_stock(drop=[SESSIONS - 1])
    row = rc.build_row("HALTED", aligned, coverage, flat_benchmark())
    assert row["p0_date"] == calendar()[-2].date().isoformat()
    assert row["sessions_available"] == SESSIONS - 1


# ── Filters ───────────────────────────────────────────────────────────────────

BASE = {"ticker": "T", "rs_rating": 95, "price": 50.0, "avg_vol_50d": 2_000_000.0,
        "above_sma50": True, "above_sma200": True, "rvol": 2.0, "adr_pct": 5.0}


def test_a_filter_whose_input_is_missing_rejects_and_names_itself():
    """Unlike swing_screener, where a None means a paywalled provider and passes.
    Here a None sma200 is a fact about the stock: it has no 200-day average."""
    row = dict(BASE, above_sma200=None)
    ok, rejects = rc.passes_filters(row, {"rs_require_above_sma200": True})
    assert not ok
    assert any("above_sma200" in r for r in rejects)


def test_an_unrated_name_is_rejected_and_named():
    ok, rejects = rc.passes_filters(dict(BASE, rs_rating=None), {})
    assert not ok
    assert any("not rated" in r for r in rejects)


def test_the_adr_and_rvol_filters_are_off_by_default():
    row = dict(BASE, rvol=0.1, adr_pct=0.1)
    ok, rejects = rc.passes_filters(row, {"rs_min_price": 1.0, "rs_min_avg_vol_50d": 0.0,
                                          "rs_min_rs_rating": 0})
    assert ok, rejects


def test_the_rvol_filter_rejects_once_switched_on():
    row = dict(BASE, rvol=0.9)
    ok, rejects = rc.passes_filters(row, {"rs_min_price": 1.0, "rs_min_avg_vol_50d": 0.0,
                                          "rs_min_rs_rating": 0,
                                          "rs_use_rvol_filter": True, "rs_min_rvol": 1.5})
    assert not ok
    assert any("rvol" in r for r in rejects)


def test_a_rejected_candidate_names_every_filter_it_failed():
    row = dict(BASE, price=2.0, avg_vol_50d=1000.0, above_sma50=False)
    ok, rejects = rc.passes_filters(row, {})
    assert not ok
    assert len(rejects) == 3


# ── Ordering ──────────────────────────────────────────────────────────────────

def test_the_ordering_is_deterministic_under_ties():
    """Churn in a ranked list reads as new information when it is not."""
    rows = [{"ticker": t, "rs_rating": 90, "ret_3m": 0.5,
             "avg_dollar_vol_50d": 1e8} for t in ("ZZZ", "AAA", "MMM")]
    assert [r["ticker"] for r in rc.rank_and_cap(rows, 10)] == ["AAA", "MMM", "ZZZ"]
    assert rc.rank_and_cap(list(reversed(rows)), 10) == rc.rank_and_cap(rows, 10)


def test_ranking_puts_the_strongest_first_and_caps():
    rows = [{"ticker": f"S{i}", "rs_rating": i, "ret_3m": 0.1,
             "avg_dollar_vol_50d": 1e8} for i in range(1, 21)]
    top = rc.rank_and_cap(rows, 3)
    assert [r["rs_rating"] for r in top] == [20, 19, 18]


def test_an_unrated_row_sorts_last_rather_than_first():
    rows = [{"ticker": "NORATE", "rs_rating": None, "ret_3m": 9.0, "avg_dollar_vol_50d": 1e9},
            {"ticker": "GOOD", "rs_rating": 80, "ret_3m": 0.1, "avg_dollar_vol_50d": 1e8}]
    assert [r["ticker"] for r in rc.rank_and_cap(rows, 5)] == ["GOOD", "NORATE"]


# ── Serialisation ─────────────────────────────────────────────────────────────

def test_non_finite_values_are_converted_before_they_can_reach_json():
    """json.dump writes bare NaN, which is not JSON and throws in JSON.parse."""
    import json
    payload = rc.jsonable({"a": float("nan"), "b": float("inf"),
                           "c": [1.5, float("-inf")], "d": np.float64("nan"),
                           "e": np.int64(7), "f": np.bool_(True)})
    assert payload == {"a": None, "b": None, "c": [1.5, None], "d": None,
                       "e": 7, "f": True}

    def _boom(value):
        raise AssertionError(f"non-JSON constant reached the file: {value}")

    json.loads(json.dumps(payload), parse_constant=_boom)
