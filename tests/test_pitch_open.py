"""Pitch-open predicate and harness checks. No warehouse and no market return."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pitch_open as P


def test_frozen_pairs():
    # (f, s): grade, same, steep, complement
    cases = {
        (3, 8): (True, True, False, False),
        (8, 3): (True, True, False, False),
        (9, 3): (True, False, True, False),
        (2, 8): (False, False, False, True),
        (0, 5): (False, False, False, False),
        (5, 5): (True, True, False, False),
    }
    for (f, s), exp in cases.items():
        grade = P.state_fs((f, s))
        same = P.is_same(f, s)
        steep = P.is_steep(f, s)
        comp = P.complement_fs((f, s))
        assert (grade, same, steep, comp) == exp
    for f in range(-5, 20):
        for s in range(-5, 20):
            grade = P.state_fs((f, s))
            assert grade == (P.is_same(f, s) or P.is_steep(f, s))
            assert not (P.is_same(f, s) and P.is_steep(f, s))
            assert grade != P.complement_fs((f, s)) or not (f > 0 and s > 0)


def test_pair_uses_tenths_not_raw_float():
    fast = {10: -50.0, 11: -49.7}
    slow = {10: -40.0, 11: -39.2}
    assert P.pair(fast, slow, 11) == (3, 8)
    assert P.state_fs(P.pair(fast, slow, 11))
    fast = {10: -20.0, 11: -19.2}
    slow = {10: -30.0, 11: -29.7}
    assert P.pair(fast, slow, 11) == (8, 3)
    assert P.is_same(8, 3) and not P.is_steep(8, 3)
    assert P.tenths(round(0.30000000000000004, 1)) == 3
    assert round(0.15, 1) == 0.1
    assert round(2.05, 1) == 2.0
    assert P.pair(fast, slow, 12) is None


def test_onset_refractory_and_prewindow():
    def pred_from(on):
        return lambda t: t in on

    assert P.fires(pred_from({580, 585})) == [585]
    assert P.fires(pred_from({584, 585, 586})) == []
    assert P.fires(pred_from({585, 590, 600})) == [585, 600]
    assert P.fires(pred_from({585, 599})) == [585]
    # A pre-window onset must not consume the 15-minute clock.
    assert P.fires(pred_from({580, 590})) == [590]


def test_missing_fill_does_not_release_the_clock():
    # The clock is the fire list itself. A later flicker stays out even when the
    # first fire has no prices, because the fill is attempted after the fire.
    fired = P.fires(lambda t: t in {585, 590})
    assert fired == [585]
    assert P.gross_at({586: (10.0, 10.0)}, 586, P.HOLD) is None
    assert P.miss_reason({586: (10.0, 10.0)}, 586, P.HOLD) == "missing_exit"


def test_next_open_markout():
    sip = {600: (100.0, 99.0), 609: (101.0, 102.0)}
    assert P.gross_at(sip, 600, 10) == pytest.approx(0.02)
    assert P.gross_at({600: (100.0, 100.0)}, 600, 10) is None
    assert P.gross_at({600: (0.0, 0.0), 609: (1.0, 1.0)}, 600, 10) is None


def test_control_lists_are_ordered_and_the_draw_is_one_choice():
    sip = {m: (10.0, 10.5) for m in range(600, 680)}
    del sip[610]
    cand = P.d_candidates(sip, 600, 10)
    assert cand == sorted(cand)
    # Hold 10 exits at c+9, so the hole at 610 also drops the 601 control.
    assert 601 not in cand and 602 in cand and 610 not in cand
    sips = {
        "AA": {601: (10.0, 10.0), 610: (10.0, 11.0)},
        "ZZ": {601: (10.0, 10.0), 610: (10.0, 11.0)},
        "MM": {601: (10.0, 10.0)},
    }
    xs = P.x_candidates(sips, ["AA", "MM", "ZZ"], "AA", 601, 10)
    assert xs == ["ZZ"]
    a = P.seeded("DAL", "2026-05-12", 601, "pitch-d").choice(cand)
    b = P.seeded("DAL", "2026-05-12", 601, "pitch-d").choice(cand)
    assert a == b


def test_empty_square_shape():
    fast = {10: -30.2, 11: -30.0}
    slow = {10: -28.0, 11: -28.0}
    assert P.empty_square(fast, slow, 11)
    full_f = {10: -10.2, 11: -10.0}
    full_s = {10: -9.0, 11: -9.0}
    assert not P.empty_square(full_f, full_s, 11)
    wide_s = {10: -10.0, 11: -10.0}
    assert not P.empty_square(fast, wide_s, 11)
    flat = {10: -30.0, 11: -30.0}
    assert not P.empty_square(flat, slow, 11)


def test_bad_line_series():
    import numpy as np
    ok = np.array([[570, 1, 2, 1, 1, 1], [572, 1, 2, 1, 1, 1]], dtype=float)
    dup = np.array([[570, 1, 2, 1, 1, 1], [570, 1, 2, 1, 1, 1]], dtype=float)
    back = np.array([[572, 1, 2, 1, 1, 1], [571, 1, 2, 1, 1, 1]], dtype=float)
    assert P.series_ok(ok)
    assert not P.series_ok(dup)
    assert not P.series_ok(back)
    assert not P.series_ok(np.array([]))


def test_canary_lifts_both_halves_by_10bp():
    rows = []
    for day in ("2026-06-02", "2026-06-03", "2026-08-03", "2026-08-04"):
        for i in range(4):
            rows.append({
                "day": day, "sym": f"S{i}", "pop": "primary",
                "gross": 0.0, "d": -0.0002 * i, "x": 0.0001, "d2": 0.0, "x2": 0.0,
                "f": 5, "s": 5,
            })
    deltas = P.canary_deltas(rows)
    assert set(deltas) == {
        "A_net", "A_lift_d", "A_lift_x", "B_net", "B_lift_d", "B_lift_x",
    }
    for v in deltas.values():
        assert v == pytest.approx(0.0010)


def test_module_does_not_import_the_live_arm():
    src = open(P.__file__).read()
    assert "import signals" not in src
    assert "import ai_entry_watch" not in src
    assert "should_arm_buy" not in src
