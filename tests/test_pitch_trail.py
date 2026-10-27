"""Pitch-trail exit checks. No warehouse and no market return."""
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pitch_open as P
import pitch_trail as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bar(o, h, l, c):
    return (float(o), float(h), float(l), float(c))


def test_source_does_not_pull_in_the_live_arm():
    src = open(os.path.join(ROOT, "tools", "studies", "pitch_trail.py")).read()
    for s in ("import ai_entry_watch", "import ai_positions", "import signals", "should_arm_buy"):
        assert s not in src


def test_prereg_has_the_round_2_sentences():
    doc = json.load(open(os.path.join(ROOT, "docs", "studies", "pitch_trail_prereg.json")))
    assert doc["schema"] == "pitch_trail_v2"
    assert doc["amended"]["review_id"] == T.REVIEW_ID
    blob = json.dumps(doc)
    assert "max(stop, round(close, 2))" in blob
    assert "including when 949 has no positive open" in blob
    assert "Same-name eligible minutes are ascending" in blob
    assert "is not a READY" in blob


def test_grade_is_the_pitch_open_grade():
    assert P.state_fs((3, 8)) and P.is_same(3, 8) and not P.is_steep(3, 8)
    assert P.state_fs((8, 3)) and P.is_same(8, 3) and not P.is_steep(8, 3)
    assert P.state_fs((9, 3)) and P.is_steep(9, 3)
    assert not P.state_fs((2, 8)) and P.complement_fs((2, 8))
    assert not P.state_fs((0, 5))
    assert P.state_fs((5, 5)) and P.is_same(5, 5)


def test_initial_stop_on_a_100_fill():
    assert T.initial_stop(100.0) == 99.4
    assert T.cent_ceil(99.4) == 99.4


def test_stop_is_tested_before_the_same_bar_high():
    # Low tags the 99.40 stop while the high is 110. The high must not raise the stop first.
    got = T.trail_exit({700: _bar(100, 110, 99.40, 105)}, 700)
    assert got is not None
    assert got[0] == pytest.approx(99.4)
    assert got[1] == 700
    assert got[2] == "stop"


def test_missing_lookback_sells():
    assert T.decide(None, None, lookback=10, eps=0.05)["sell"] == "yes"
    assert T.decide([0.0] * 9, [0.0] * 10, lookback=10, eps=0.05)["sell"] == "yes"


def test_red_close_clears_idle_so_the_leash_does_not_fire():
    # High is not above the fill, so idle would be 60. The close is under the fill, so the leash stays off.
    bars = {
        900: _bar(100, 100, 99.50, 99.90),
        901: _bar(100.2, 100.5, 100.1, 100.4),
    }
    got = T.trail_exit(bars, 900)
    assert got is not None
    assert got[1] > 900
    assert not (got[1] == 900 and got[2] == "leash")


def test_breakeven_can_raise_before_the_trail_is_armed():
    # +0.20 on a 100 fill is 0.04R, under the 0.06R arm, and 0.20% of price, over the breakeven line.
    bars = {
        700: _bar(100, 100.20, 100.10, 100.15),
        701: _bar(100.10, 100.12, 100.01, 100.05),
    }
    got = T.trail_exit(bars, 700)
    assert got is not None
    assert got[0] == pytest.approx(100.01)
    assert got[1] == 701
    assert got[2] == "stop"


def test_held_leash_stop_does_not_lower():
    assert T.held_leash_stop(100.20, 100.04) == 100.20
    assert T.held_leash_stop(99.40, 100.03) == 100.03


def test_held_leash_blocks_and_the_trade_stays_open():
    bars = {}
    for i, m in enumerate(range(400, 550)):
        px = 80.0 + i * 0.01
        bars[m] = _bar(px, px + 0.5, px - 0.5, px + 0.4)
    bars[550] = _bar(100, 100.20, 99.80, 100.10)
    bars[551] = _bar(100.03, 100.04, 100.02, 100.03)
    got = T.trail_exit(bars, 550)
    assert got is not None
    assert got[2] == "flatten"
    assert got[1] == 949
    assert got[0] == pytest.approx(100.03)


def test_949_with_no_open_uses_the_latest_prior_close():
    bars = {
        900: _bar(100, 100, 99.50, 10),
        910: (0, 0, 0, 15),
        920: (0, 0, 0, 20),
    }
    got = T.trail_exit(bars, 900)
    assert got == pytest.approx((20, 949, "flatten")) or got[:2] == (20, 949)
    assert got[0] == 20
    assert got[1] == 949
    assert got[2] == "flatten"
    # A later print after 949 is not the flatten.
    bars[955] = _bar(100, 100, 99, 99)
    got = T.trail_exit(bars, 900)
    assert got[0] == 20


def test_no_positive_close_in_the_window_is_no_position():
    assert T.trail_exit({900: _bar(100, 100, 99.50, 0)}, 900) is None
    assert T.flatten_price({900: _bar(100, 100, 99, 0), 890: _bar(50, 50, 50, 50)}, 900) is None


def test_same_name_minutes_are_ascending_and_capped():
    bars = {}
    bars[610] = _bar(10, 10.2, 9.8, 10.1)
    bars[601] = _bar(10, 10.2, 9.8, 10.1)
    bars[605] = (0, 1, 1, 1)
    bars[602] = _bar(10, 10.2, 9.8, 10.1)
    bars[660] = _bar(10, 10.2, 9.8, 10.1)
    bars[661] = _bar(10, 10.2, 9.8, 10.1)
    got = T.same_name_minutes(bars, 600, T.close_minutes(bars))
    assert got == [601, 602, 610, 660]
    late = {m: _bar(10, 10.2, 9.8, 10.1) for m in (901, 949, 960)}
    assert T.same_name_minutes(late, 900, T.close_minutes(late)) == [901, 949]


def test_other_names_are_sorted_and_skip_a_dead_open():
    sips = {
        "B": {600: _bar(10, 11, 9, 10.5)},
        "A": {600: _bar(10, 11, 9, 10.5)},
        "C": {600: (0, 1, 1, 1)},
        "D": {600: _bar(10, 11, 9, 10.5)},
    }
    closes = {k: T.close_minutes(v) for k, v in sips.items()}
    assert T.other_names(sips, ["B", "A", "D", "C"], "B", 600, closes) == ["A", "D"]


def test_fill_ignores_the_15_minute_clock():
    def classify(t):
        return ("fill", 1.0, t + 1, "leash")

    got = [g[1] for g in T.run_clock([585, 590], classify)]
    assert got == [585, 590]


def test_a_miss_sets_the_15_minute_clock():
    def classify(t):
        if t == 585:
            return ("miss", "missing_entry")
        return ("fill", 1.0, t + 1, "flatten")

    got = list(T.run_clock([585, 590, 600], classify))
    assert [g[1] for g in got if g[0] == "fill"] == [600]
    assert ("miss", 585, "missing_entry") in got


def test_a_fill_locks_through_the_exit_minute():
    def classify(t):
        if t == 585:
            return ("fill", 1.0, 600, "flatten")
        return ("fill", 1.0, t + 1, "leash")

    got = [g[1] for g in T.run_clock([585, 600, 601], classify) if g[0] == "fill"]
    assert got == [585, 601]


def test_prewindow_does_not_set_the_clock():
    def classify(t):
        return ("fill", 1.0, t + 1, "leash")

    got = [g[1] for g in T.run_clock([580, 585], classify) if g[0] == "fill"]
    assert got == [585]


def test_onset_is_the_turn():
    pred = lambda t: t in {584, 585, 586, 600}
    assert T.onset_minutes(pred) == [600]


def test_cached_window_matches_the_pasted_reader():
    rng = random.Random(7)
    hlc = []
    for i in range(160):
        px = 50.0 + i * 0.02 + rng.random()
        if i == 140:
            hlc.append((px, px, px))
        else:
            hlc.append((px + 0.4, px - 0.3, px + 0.1))
    bars = {i: (c, h, l, c) for i, (h, l, c) in enumerate(hlc)}
    cache = T.build_hold_cache(bars)
    price = 60.0
    fast, slow = T.windows_before(cache, len(hlc), price)
    assert fast == pytest.approx(T.line_window(hlc, price, 21, 7.0, 10))
    assert slow == pytest.approx(T.line_window(hlc, price, 112, 3.0, 10))
    # A prefix too short to smooth the slow line is a missing lookback.
    short = hlc[:30]
    short_bars = {i: (c, h, l, c) for i, (h, l, c) in enumerate(short)}
    sf, ss = T.windows_before(T.build_hold_cache(short_bars), len(short), price)
    assert sf == pytest.approx(T.line_window(short, price, 21, 7.0, 10))
    assert ss is None and T.line_window(short, price, 112, 3.0, 10) is None
    assert T.decide(sf, ss, lookback=10, eps=0.05)["sell"] == "yes"
