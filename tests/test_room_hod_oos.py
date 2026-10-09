import importlib.util, os, sys
_d = os.path.join(os.path.dirname(__file__), "..", "tools", "studies")
sys.path.insert(0, _d)
_s = importlib.util.spec_from_file_location("room_hod_oos", os.path.join(_d, "room_hod_oos.py"))
R = importlib.util.module_from_spec(_s)
_s.loader.exec_module(R)
N = R.N

DAY = "2026-07-15"


def row(minute, o, h, c):
    return (N.day_ts(DAY, minute), o, h, c, 1000.0)


def test_room_uses_running_high_through_the_bar_only():
    rows = [row(570, 100, 100, 100), row(571, 100, 110, 107), row(572, 107, 107, 100), row(573, 100, 120, 119)]
    assert round(R.room_of(rows, 2), 4) == round((1 - 100 / 110) * 100, 4)     # the 120 high later must not leak
    assert R.room_of(rows, 0) == 0


def test_cut_is_inclusive_at_2744():
    rows = [row(570, 100, 100, 100), row(571, 100, 100, 100 * (1 - 0.02744))]
    assert R.room_of(rows, 1) >= R.CUT - 1e-9


def test_60_min_hold_exit():
    rows = [row(600 + k, 100, 100, 100 + k * 0.01) for k in range(80)]
    te, tx, g = R.outcome(rows, 0, DAY)
    assert N.et_min(te) == 601 and N.et_min(tx) == 661


def test_60_min_hole_rule():
    rows = [row(600, 100, 100, 100), row(601, 100, 100, 100), row(690, 100, 100, 101)]   # next bar 89 min after entry
    assert R.outcome(rows, 0, DAY) is None
