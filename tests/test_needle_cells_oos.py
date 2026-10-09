import importlib.util, os, sys
_p = os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "needle_cells_oos.py")
_s = importlib.util.spec_from_file_location("needle_cells_oos", _p)
N = importlib.util.module_from_spec(_s)
_s.loader.exec_module(N)

DAY = "2026-07-15"


def row(minute, o, c):
    return (N.day_ts(DAY, minute), o, max(o, c), c, 1000.0)


def test_edges_are_half_open():
    assert N.in_c2(4.718, 2.433) and not N.in_c2(4.717, 3.0) and not N.in_c2(5.0, 3.481)
    assert N.in_c1(1.583, 655) and N.in_c1(4.7, 715) and not N.in_c1(4.718, 700) and not N.in_c1(2.0, 716)


def test_pm_range_one_bar_is_enough():
    assert N.pm_range_of([]) is None
    assert round(N.pm_range_of([(0, 103.0, 100.0)]), 6) == 3.0


def test_outcome_enters_next_open_exits_last_bar_at_or_before_30min():
    rows = [row(600 + k, 100 + k * 0.1, 100 + k * 0.1) for k in range(40)]
    te, tx, g = N.outcome(rows, 0, DAY)
    assert te == rows[1][0] and tx == rows[31][0]
    assert round(g, 3) == round((rows[31][3] / rows[1][1] - 1) * 1e4, 3)


def test_outcome_drops_a_hole_and_respects_the_1555_cap():
    rows = [row(600, 100, 100), row(601, 100, 100), row(650, 101, 101)]       # nothing within 45 min after 10:31
    assert N.outcome(rows, 0, DAY) is None
    rows = [row(940 + k, 100, 100 + k) for k in range(20)]                   # 15:40.. ; cap at 15:55
    te, tx, g = N.outcome(rows, 0, DAY)
    assert N.et_min(tx) == 955


def test_no_next_bar_within_5_min_is_dropped():
    assert N.outcome([row(600, 100, 100), row(610, 100, 100)], 0, DAY) is None


def mom(sym, hour, gross, c2):
    return {"day": DAY, "nd": (sym, DAY), "hour": hour, "gross": gross, "c2": c2}


def test_control_excludes_own_name_day_and_averages_moment_then_name_day_then_day():
    ms = [mom("A", 10, 50, True), mom("A", 10, 0, False),          # A's own non-cell moment must not be its control
          mom("A", 11, 30, True),
          mom("B", 10, 10, False), mom("B", 11, 20, False)]
    r = N.score_cell(ms, "c2", 0.0)
    # A@10: 50 - mean(B@10 = 10) = 40 ; A@11: 30 - 20 = 10 ; name-day mean 25 ; day mean 25
    assert r["day_diff"][DAY] == 25 and r["day_net"][DAY] == 40 and r["n_nd"] == 1


def test_drop_top_name_days():
    ms = [mom("A", 10, 100, True), mom("C", 10, 0, True), mom("B", 10, 0, False)]
    r = N.score_cell(ms, "c2", 0.0, drop_nd={("A", DAY)})
    assert r["n_nd"] == 1 and r["day_diff"][DAY] == 0
