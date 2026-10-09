import importlib.util, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
_p = os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "day_hold.py")
_s = importlib.util.spec_from_file_location("day_hold", _p)
DH = importlib.util.module_from_spec(_s)
_s.loader.exec_module(DH)
import bars_structure as BS

DAY = "2026-09-15"


def bar(hm, o, h, l, c, v=100000):
    return [BS.et_ts(DAY, hm // 60, hm % 60), o, h, l, c, v]


def day(path_after):
    bars = [bar(570 + i, 100, 100.5, 99.8, 100.2) for i in range(59)]            # 09:30..10:28
    bars.append(bar(629, 100.2, 100.4, 100.1, 100.3))                             # 10:29 -> P
    bars += [bar(630 + i, *x) for i, x in enumerate(path_after)]
    return bars


def test_morning_uses_bars_through_1029_only():
    b = day([(150, 150, 150, 150)])                                               # absurd 10:30 bar must not leak
    f = DH.morning(b)
    assert f["P"] == 100.3 and f["MH"] == 100.5 and f["ML"] == 99.8 and not f["P_fallback"]


def test_stop_wins_and_gap_fill():
    b = day([(100, 100.1, 99.9, 100), (96.0, 96.5, 95.5, 96.2)])
    g, why = DH.simulate(b, ml=99.0)
    assert why == "stop" and round(g, 1) == round(1e4 * (96.0 / 100 - 1), 1)    # min(stop 97, open 96)


def test_knife_below_morning_low_exits_next_open():
    b = day([(100, 100.2, 99.9, 100.1), (100, 100, 99.5, 99.6), (99.7, 99.8, 99.6, 99.7)])
    g, why = DH.simulate(b, ml=99.8)
    assert why == "knife_ml" and round(g, 1) == round(1e4 * (99.7 / 100 - 1), 1)


def test_time_exit_and_no_knife():
    path = [(100, 100.3, 99.9, 100.2)] * (954 - 630 + 1)
    g, why = DH.simulate(day(path), ml=50)
    assert why == "time" and round(g, 1) == 20.0


def test_qualifies_filters():
    f = {"P": 103, "O": 101, "MH": 103.5, "ML": 100, "dv": 6e6}
    assert DH.qualifies(f, 100, True)
    assert not DH.qualifies(f, 100, False)                                        # below SMA50
    assert not DH.qualifies({**f, "P": 101.5}, 100, True)                         # chg < 2%
    assert not DH.qualifies({**f, "MH": 105}, 100, True)                          # not holding near high
    assert not DH.qualifies({**f, "dv": 4e6}, 100, True)


def test_giveback_and_breakeven_and_resist():
    up = [(100, 102.5, 99.95, 102.4), (102.4, 102.5, 102.3, 102.4), (102.4, 102.4, 101.0, 101.1)]
    g, why = DH.simulate(day(up), ml=99.0, giveback=True)
    assert why == "giveback" and round(g, 1) == round(1e4 * (101.25 / 100 - 1), 1)    # 100 + 0.5 x 2.5
    be = [(100, 101.6, 99.95, 101.5), (101.5, 101.5, 101.4, 101.45), (101.0, 101.0, 99.5, 99.6)]
    g, why = DH.simulate(day(be), ml=99.0, breakeven=True)
    assert why == "breakeven" and round(g, 1) == 0.0
    g, why = DH.simulate(day([(100, 100.2, 99.95, 100.1), (100.1, 100.9, 100.0, 100.8)]), ml=99.0, resist=100.5)
    assert why == "resist" and round(g, 1) == 50.0
