"""Marked-turn checks. No warehouse and no market return."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pitch_open as P
import pitch_trough as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 600


def _lines(fv, sv, df, ds, pdf, pds, cf5, cs5, t=T0):
    fast = {t: fv, t - 1: fv - df, t - 2: fv - df - pdf, t - 5: fv - cf5}
    slow = {t: sv, t - 1: sv - ds, t - 2: sv - ds - pds, t - 5: sv - cs5}
    return fast, slow


def _turn(fv, sv, df, ds, pdf=0.0, pds=0.0, cf5=0.25, cs5=0.25):
    fast, slow = _lines(fv, sv, df, ds, pdf, pds, cf5, cs5)
    return M.turn(fast, slow, T0), P.pair(fast, slow, T0), P.pair(fast, slow, T0 - 1)


def test_source_does_not_pull_in_the_live_arm():
    src = open(os.path.join(ROOT, "tools", "studies", "pitch_trough.py")).read()
    for s in ("import ai_entry_watch", "import ai_positions", "import signals", "should_arm_buy"):
        assert s not in src
    assert "info_for" not in src
    assert 'res["info"]' not in src


def test_prereg_is_one_grade_and_ready():
    doc = json.load(open(os.path.join(ROOT, "docs", "studies", "pitch_trough_prereg.json")))
    assert doc["schema"] == "pitch_trough_v1"
    assert doc["ready"]["review_id"] == M.READY_ID
    blob = json.dumps(doc)
    assert "pop \\\"primary\\\"" in blob or 'pop "primary"' in blob
    assert "lock = signal + 14" in blob
    assert "never pitch_open's (open, close) pair" in blob
    assert "there are no information cells" in blob


def test_frozen_shape_and_trough():
    ok, fs, prev = _turn(-40, -20, 0.5, 0.5)
    assert fs == (5, 5) and prev == (0, 0) and ok
    ok, fs, _ = _turn(-40, -20, 0.9, 0.3)
    assert fs == (9, 3) and ok
    ok, fs, _ = _turn(-40, -20, 0.8, 0.3)
    assert fs == (8, 3) and ok
    ok, fs, _ = _turn(-40, -20, 0.3, 0.8)
    assert fs == (3, 8) and ok
    ok, fs, _ = _turn(-40, -20, 0.2, 0.8)
    assert fs == (2, 8) and not ok
    ok, fs, _ = _turn(-20, -40, 0.9, 0.3)
    assert fs == (9, 3) and not ok
    ok, fs, _ = _turn(-20, -40, 0.5, 0.5)
    assert fs == (5, 5) and not ok
    ok, fs, _ = _turn(-40, -20, 0.0, 0.5)
    assert fs == (0, 5) and not ok
    ok, _, prev = _turn(-40, -20, 0.5, 0.5, pdf=-0.1, pds=0.0)
    assert prev == (-1, 0) and ok
    ok, _, prev = _turn(-40, -20, 0.5, 0.5, pdf=0.0, pds=-0.3)
    assert prev == (0, -3) and ok
    ok, _, prev = _turn(-40, -20, 0.5, 0.5, pdf=0.1, pds=0.0)
    assert prev == (1, 0) and not ok
    ok, _, prev = _turn(-40, -20, 0.5, 0.5, pdf=0.0, pds=0.1)
    assert prev == (0, 1) and not ok


def test_five_minute_permission():
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, 0.25, 0.25)
    assert M.pitch5(fast, slow, T0) == (1, 1)
    assert M.turn(fast, slow, T0)
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, 0.20, 0.25)
    assert M.pitch5(fast, slow, T0)[0] == 0
    assert not M.turn(fast, slow, T0)
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, -0.25, 0.25)
    assert M.pitch5(fast, slow, T0)[0] == -1
    assert not M.turn(fast, slow, T0)
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, 0.25, 0.25)
    del fast[T0 - 5]
    assert M.pitch5(fast, slow, T0) is None
    assert not M.turn(fast, slow, T0)
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, 0.25, 0.25)
    fast[T0 - 6] = fast.pop(T0 - 5)
    slow[T0 - 6] = slow.pop(T0 - 5)
    assert not M.turn(fast, slow, T0)
    fast, slow = _lines(-40, -20, 0.5, 0.5, 0.0, 0.0, 0.25, 0.25)
    assert (T0 - 4) not in fast and (T0 - 3) not in slow
    assert M.turn(fast, slow, T0)


def test_a_fill_locks_when_the_control_is_missing():
    seen = []

    def classify(t):
        seen.append(t)
        if t == 600:
            return ("fill", 1.0, 620, "leash")
        return ("fill", 1.0, t + 5, "leash")

    scored, other = M.take([600, 610, 621], classify, lambda t: None)
    assert scored == []
    assert seen == [600, 621]
    assert ("control", 600) in other and ("control", 621) in other


def test_a_miss_locks_fourteen_minutes():
    seen = []

    def classify(t):
        seen.append(t)
        return ("miss", "missing_entry")

    M.take([600, 610, 614, 615], classify, lambda t: (0, 0, 0, 0))
    assert seen == [600, 615]


def test_decide_rejects_a_two_value_bar():
    assert M.decide({601: (10.0, 11.0)}, 600, lambda m: (11.0, 602, "stop"))[0] == "miss"
    assert M.decide({601: (10.0, 10.5, 9.5, 10.2)}, 600, lambda m: None)[1] == "missing_exit"
    kind = M.decide({601: (10.0, 10.5, 9.5, 10.2)}, 600, lambda m: (10.2, 602, "stop"))
    assert kind[0] == "fill" and kind[1] == 10.2


def test_check_rows_rejects_a_fast_line_already_above():
    row = {
        "pop": "primary", "f": 5, "s": 5, "pf": 0, "ps": 0, "f5": 1, "s5": 1,
        "fast_under": False, "gross": 0.0, "d": 0.0, "d2": 0.0, "x": 0.0, "x2": 0.0,
    }
    try:
        M.check_rows([row])
    except SystemExit:
        return
    raise AssertionError("row passed")
