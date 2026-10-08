"""Tests for tools/studies/bars_structure.py (pure; synthetic bars, no network)."""
from __future__ import annotations

import random

from _hist_synth import DAY, T, bar, bars60, expand, flat, from15
import bars_structure as BS


# ------------------------------------------------------------------ aggregation and completion
def test_aggregate_aligns_to_0930_and_60min_half_bar():
    b1 = flat(100, T(9, 30), T(16, 0))
    b15 = BS.aggregate(b1, DAY, 15)
    assert len(b15) == 26 and b15[0].start == T(9, 30) and b15[0].end == T(9, 45)
    b60 = BS.aggregate(b1, DAY, 60)
    assert len(b60) == 7
    assert b60[-1].start == T(15, 30) and b60[-1].end == T(16, 0)   # the half bar is its own bar


def test_aggregate_ohlc_and_premarket_excluded():
    rows = [[T(9, 0), 1, 999, 0.5, 1, 1]] + [[b.start, b.o, b.h, b.l, b.c, b.v]
                                              for b in expand(T(9, 30), 10, 12, 9, 11, 15)]
    b1 = BS.minute_bars(rows, DAY)
    assert all(b.start >= T(9, 30) for b in b1)
    b = BS.aggregate(b1, DAY, 15)[0]
    assert (b.o, b.h, b.l, b.c) == (10, 12, 9, 11)
    pre = BS.minute_bars(rows, DAY, rth=False)
    assert [b.h for b in pre] == [999]


def test_forming_bar_is_never_completed():
    b15 = BS.aggregate(flat(100, T(9, 30), T(11, 0)), DAY, 15)
    assert [b.end for b in BS.completed(b15, T(10, 7))] == [T(9, 45), T(10, 0)]
    assert BS.completed(b15, T(10, 15))[-1].end == T(10, 15)        # end <= t is completed


def test_htf15_ignores_forming_bar():
    spec = [(100, 101, 99, 100), (100, 102, 99.5, 101), (101, 103, 100, 102), (102, 101, 95, 96)]
    b15 = BS.aggregate(from15(spec), DAY, 15)
    assert BS.htf15_up(b15, T(10, 14)) is None     # only 2 completed bars
    assert BS.htf15_up(b15, T(10, 29)) is True     # B4 (10:15-10:30) is forming: not read
    assert BS.htf15_up(b15, T(10, 30)) is False    # once completed it breaks the structure


# ------------------------------------------------------------------ swings
def _bars(hl, t0=None, size=60):
    t0 = t0 or T(9, 30)
    return [bar(t0 + i * size * 60, (h + l) / 2, h, l, (h + l) / 2, size) for i, (h, l) in enumerate(hl)]


def test_swing_strict_and_known_at_end_of_i_plus_2():
    bs = _bars([(10, 5), (11, 6), (12, 7), (11, 6), (10, 5)], size=15)
    sw = BS.swings(bs)
    assert [(s.kind, s.i, s.price) for s in sw] == [("H", 2, 12)]
    assert sw[0].known_ts == bs[4].end
    assert BS.swings_at(bs, bs[4].end - 1) == []           # not visible before known_ts
    assert len(BS.swings_at(bs, bs[4].end)) == 1


def test_swing_equal_high_is_not_a_swing():
    bs = _bars([(10, 5), (12, 6), (12, 7), (11, 6), (10, 5)], size=15)
    assert [s for s in BS.swings(bs) if s.kind == "H"] == []


def test_60min_swings_across_sessions():
    d1, d2 = "2026-09-14", DAY
    a = bars60(d1, [(100, 99, 98, 99)] * 4 + [(100, 101, 97, 100), (100, 103, 99, 101), (101, 104, 100, 102)])
    b = bars60(d2, [(102, 103, 100, 101), (101, 102, 99, 100)])
    s = BS.concat_sessions([a, b])
    hs = [x for x in BS.swings_at(s, b[1].end) if x.kind == "H"]
    assert hs and hs[-1].start == a[-1].start           # the 15:30-16:00 half bar is a swing point
    assert hs[-1].known_ts == b[1].end
    assert [x for x in BS.swings_at(s, b[1].end - 1) if x.start == a[-1].start] == []


def test_up60():
    S = BS.Swing
    sw = [S("H", 1, 10, 0, 0, 0), S("L", 2, 8, 0, 0, 0), S("H", 3, 11, 0, 0, 0), S("L", 4, 9, 0, 0, 0)]
    assert BS.up60(sw)
    assert not BS.up60(sw[:3])
    sw2 = sw[:3] + [S("L", 4, 7.5, 0, 0, 0)]
    assert not BS.up60(sw2)


# ------------------------------------------------------------------ predicates
def test_rejection_momentum_pin():
    assert BS.rejection_bar(bar(0, 10.0, 10.2, 9.0, 10.1))
    assert not BS.rejection_bar(bar(0, 10.0, 10.2, 9.9, 10.1))       # small wick
    assert not BS.rejection_bar(bar(0, 10.0, 10.0, 10.0, 10.0))      # range 0
    prev = bar(0, 10, 10.5, 9.8, 10.2)
    assert BS.momentum_bar(bar(60, 10.2, 11.0, 10.15, 10.95), prev)
    assert not BS.momentum_bar(bar(60, 10.2, 11.0, 10.15, 10.45), prev)   # close not > prev high
    assert not BS.momentum_bar(bar(60, 10.6, 11.0, 10.0, 10.9), prev)     # body < 60%
    assert BS.pin_bar(bar(0, 10.0, 10.1, 9.0, 10.05))
    assert not BS.pin_bar(bar(0, 9.6, 10.1, 9.0, 9.7))                    # wick 60% but close < upper third


def test_fvg_known_at_end_of_k_plus_1():
    bs = _bars([(10, 9), (11.5, 10.2), (12, 10.6)], size=5)
    f = BS.bullish_fvg(bs, 1)
    assert f == (10, 10.6, bs[2].end)
    assert BS.bullish_fvg(bs[:2], 1) is None                          # k+1 not there yet
    bs2 = _bars([(10, 9), (11.5, 10.2), (12, 9.9)], size=5)
    assert BS.bullish_fvg(bs2, 1) is None


# ------------------------------------------------------------------ EMA / HTF_UP
def test_htf60_requires_warmup_and_uses_completed_bars():
    days = ["2026-08-%02d" % d for d in range(3, 29)]
    series = []
    px = 100.0
    for d in days:
        spec = []
        for _ in range(7):
            px *= 1.002
            spec.append((px, px * 1.001, px * 0.999, px))
        series += bars60(d, spec)
    assert BS.htf60_up(series[:100], series[99].end) is None           # < 140 bars of warm-up
    assert BS.htf60_up(series, series[-1].end) is True
    down = series[:-3] + [b._replace(c=b.c * 0.9, l=b.l * 0.9) for b in series[-3:]]
    assert BS.htf60_up(down, down[-1].end) is False


def test_ema_seeded_at_first():
    assert BS.ema([10, 10, 10]) == [10, 10, 10]
    e = BS.ema([10, 20])
    assert e[0] == 10 and abs(e[1] - (10 + 2 / 21 * 10)) < 1e-12


# ------------------------------------------------------------------ name_history event and T1
def _day_bars(spec_min):
    """[(hh, mm, o, h, l, c)] -> 1-min bars"""
    return [bar(T(hh, mm), o, h, l, c) for hh, mm, o, h, l, c in spec_min]


def test_first_new_high_threshold_and_own_high_excluded():
    b1 = flat(100, T(9, 30), T(10, 29)) + [bar(T(10, 29), 100, 100.5, 100, 100.05)]  # closes 10:30, < 100.1
    b1 += [bar(T(10, 30), 100.05, 100.7, 100.05, 100.61)]                             # 100.61 >= 1.001 x 100.5
    ev = BS.first_new_high(b1, prior_close=99.0)
    assert ev and ev["t"] == T(10, 31) and ev["h_prev"] == 100.5
    b1[-1] = bar(T(10, 30), 100.05, 100.7, 100.05, 100.60)                             # 100.60 < 100.6005
    assert BS.first_new_high(b1, prior_close=99.0) is None
    # bar 10:29 (close 10:30) would be in window but its close is not >= 1.001 x prior high (own high excluded)
    b2 = flat(100, T(9, 30), T(10, 29)) + [bar(T(10, 29), 100, 100.2, 100, 100.11)]
    ev2 = BS.first_new_high(b2, prior_close=99.0)
    assert ev2 and ev2["t"] == T(10, 30)


def test_first_new_high_needs_prior_close_condition_and_window():
    b1 = flat(100, T(9, 30), T(10, 29)) + [bar(T(10, 29), 100, 100.2, 100, 100.11)]
    assert BS.first_new_high(b1, prior_close=99.8) is None           # 100.2 < 1.005 x 99.8
    assert BS.first_new_high(b1, prior_close=99.6) is not None       # 100.2 >= 1.005 x 99.6
    early = flat(100, T(9, 30), T(10, 0)) + [bar(T(10, 0), 100, 101, 100, 100.9)] + flat(100.9, T(10, 1), T(11, 0))
    assert BS.first_new_high(early, prior_close=99.0) is None         # 10:01 close is before 10:30


def test_followthrough_race_rules():
    ev = {"close": 100.0, "t": T(11, 0)}
    up = [bar(T(11, 0), 100, 100.2, 99.9, 100.1)]
    assert BS.followthrough(up, ev, DAY) is True
    both = [bar(T(11, 0), 100, 100.2, 99.6, 100.1)]
    assert BS.followthrough(both, ev, DAY) is False                     # a bar doing both = failure
    late = flat(100, T(11, 0), T(11, 30)) + [bar(T(11, 30), 100, 100.5, 100, 100.4)]
    assert BS.followthrough(late, ev, DAY) is False                     # outside 30 min
    ev2 = {"close": 100.0, "t": T(15, 10)}
    capped = flat(100, T(15, 10), T(15, 30)) + [bar(T(15, 30), 100, 100.5, 100, 100.4)]
    assert BS.followthrough(capped, ev2, DAY) is False                  # window ends by 15:30


def test_lag1_pairs_skip_missing_buckets():
    spec = [(100, 101, 99, 100.5), (100.5, 101, 100, 101), (101, 102, 100.5, 101.5)]
    b1 = from15(spec)
    b1 = [b for b in b1 if not (T(9, 45) <= b.start < T(10, 0))]      # bucket 2 missing
    p = BS.lag1_pairs(BS.aggregate(b1, DAY, 15), 15)
    assert p == []


def test_pearson_and_slope():
    pts = [(x, 2 * x + 1) for x in range(10)]
    assert abs(BS.pearson(pts) - 1) < 1e-12 and abs(BS.ols_slope(pts) - 2) < 1e-12


# ------------------------------------------------------------------ round numbers
def test_rn_groups_examples():
    assert BS.rn_group(24.97, 24.90) == "BELOW"          # 25.00 within 0.15% above (0.12%)
    assert BS.rn_group(25.00, 24.95) == "ABOVE"          # a close exactly on a level is ABOVE
    assert BS.rn_group(25.10, 24.99) == "ABOVE"
    assert BS.rn_group(25.10, 25.02) == "NEUTRAL"
    assert BS.rn_group(74.95, 74.0) == "BELOW"           # $1 grid at $50-100: 75 within 0.067%
    assert BS.rn_group(74.95, 73.9) == "BELOW"           # BELOW takes precedence over ABOVE (74 crossed)
    assert BS.rn_group(150.2, 149.5) == "ABOVE"          # $5 grid: 150 crossed
    assert BS.rn_group(151.0, 150.5) == "NEUTRAL"
    assert BS.rn_group(149.9, 148.0) == "BELOW"


def test_rn_groups_exclusive_and_exhaustive():
    rng = random.Random(3)
    for _ in range(20000):
        c = round(rng.uniform(10, 400), 2)
        h = round(c * (1 - rng.uniform(0.0005, 0.01)), 2)
        for grid in ("primary", "whole", "half"):
            for band in (0.0010, 0.0015, 0.0025):
                g = BS.rn_group(c, h, band, grid)
                assert g in ("BELOW", "ABOVE", "NEUTRAL")
                # check against a brute-force definition
                step, off = ((BS.rn_step(c), 0.0) if grid == "primary" else (1.0, 0.5 if grid == "half" else 0.0))
                levels = [round(k * step + off, 4) for k in range(int(h / step) - 2, int(c / step) + 3)]
                above = min(L for L in levels if L > c + 1e-9)
                below = (above - c) <= band * c + 1e-9
                crossed = any(h + 1e-9 < L <= c + 1e-9 for L in levels)
                want = "BELOW" if below else ("ABOVE" if crossed else "NEUTRAL")
                assert g == want, (c, h, grid, band)


def test_level_info_confluence():
    li = BS.level_info(49.95, [50.05, 49.99, 51.0, 40.0])
    assert abs(li["dist"] - (49.99 / 49.95 - 1)) < 1e-12
    assert li["confluence"] == 2      # 49.99 and the $50 round number are within 0.20% (50.05 is 0.2002%)


def test_s5_levels_known_at_t_only():
    d1 = "2026-09-14"
    a = bars60(d1, [(101, 102, 100.5, 101)] * 3 + [(101, 101.5, 99, 100), (100, 101, 99.5, 100.5),
                                                    (100.5, 101, 99.8, 100.6), (100.6, 101, 99.9, 100.8)])
    lv = BS.s5_levels(a, a[-1].end, DAY, [d1], prior_rth_low=98.0)
    assert 99 in lv and 98.0 in lv
    assert 99 not in BS.s5_levels(a, a[5].end - 1, DAY, [d1], prior_rth_low=98.0)   # known at end of i+2
    assert 99 in BS.s5_levels(a, a[5].end, DAY, [d1], prior_rth_low=98.0)
    assert 99 not in BS.s5_levels(a, a[-1].end, DAY, [], prior_rth_low=98.0)        # not in the 5 sessions


def test_atr_sma_vol():
    d = [(10, 11, 9, 10)] * 15
    assert BS.atr14(d) == 2.0
    assert BS.sma(list(range(50))) == 24.5
    assert BS.realized_vol([10.0] * 21) == 0.0


def test_level_info_confluence_dedupes_equal_levels():
    """review A note: the 20-session high equal to one of the prior 5 highs (or a level ON a round number) is one
    level, not two - levels are deduped to the cent before counting."""
    li = BS.level_info(49.95, [49.99, 49.99, 49.9900001, 50.0, 40.0])
    assert li["confluence"] == 2      # 49.99 once and the $50 round number once (50.0 is the same level)
