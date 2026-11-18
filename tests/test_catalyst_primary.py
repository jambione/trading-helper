import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import catalyst_primary as C  # noqa: E402


def _flat(n=120, px=100.0, rng=0.1):
    o = np.full(n, px); c = np.full(n, px); h = np.full(n, px + rng / 2); l = np.full(n, px - rng / 2)
    return o, h, l, c


def test_vol_exit_target_stop_and_time():
    o, h, l, c = _flat()                       # ATR 0.1 -> stop 99.8, target 100.4
    h2 = h.copy(); h2[25] = 100.5
    assert abs(C.vol_exit(o, h2, l, c, 20) - 40.0) < 1e-6
    l2 = l.copy(); l2[25] = 99.7; h3 = h.copy(); h3[25] = 100.5
    assert abs(C.vol_exit(o, h3, l2, c, 20) + 20.0) < 1e-6      # both in one minute -> stop first
    assert abs(C.vol_exit(o, h, l, c, 20)) < 1e-6               # neither -> flat at the 60th close
    assert C.vol_exit(o, h, l, c, 100) is None                  # no room for the hold
    assert C.vol_exit(o, h, l, c, 5) is None


def test_vol_pace_and_active():
    v = np.full(390, 100.0)
    assert abs(C.vol_pace(v, 20, 39000.0) - 1.0) < 1e-9          # 1500 / (39000 * 15/390)
    assert C.vol_pace(v, 10, 39000.0) is None and C.vol_pace(v, 20, 0) is None
    assert C.active("NEWS", 40, 15, None) and not C.active("NEWS", 50, 15, None) and not C.active("NEWS", 10, 15, None)
    assert C.active("VOL", 40, None, 3.0) and not C.active("VOL", 40, None, 2.9)
    assert C.active("BOTH", 40, 30, 3.5) and not C.active("BOTH", 40, None, 3.5)


def test_sample_caps_and_is_deterministic():
    rows = [{"group": "VOL", "mode": "wr", "day": "2026-05-01", "sym": "S%d" % i, "t": 20} for i in range(C.CAP + 50)]
    a, b = C.sample(rows), C.sample(list(reversed(rows)))
    assert len(a) == C.CAP and {r["sym"] for r in a} == {r["sym"] for r in b}


def test_verdict():
    assert C.verdict({"n": 10})[0] == "UNDERPOWERED"
    assert C.verdict({"n": 400, "mean_net": 3.0, "t_day": 2.7, "half1": 1.0, "half2": 2.0})[0] == "PASS"
    assert C.verdict({"n": 400, "mean_net": 3.0, "t_day": 2.3, "half1": 1.0, "half2": 2.0})[0] == "FAIL"


def test_loose_ok_open_box():
    f = np.array([-60.0, -55.0, -49.0, -48.0, -48.0, -70.0])
    assert not C.loose_ok(f, 1)            # rising but below -50
    assert C.loose_ok(f, 2) and C.loose_ok(f, 3)
    assert not C.loose_ok(f, 4)            # flat
    assert not C.loose_ok(f, 0)
    assert C.T_CRIT == 2.64 and C.DECIDE_MODES == ("wr", "loose")


def test_rates_per_10m():
    rows = [{"group": "VOL", "mode": "wr"}] * 32
    span = (C.T_LAST - C.T_FIRST + 1) / 10
    assert abs(C.rates(rows, 1)["VOL|wr"] - round(32 / span, 3)) < 1e-9


def test_direction_confirmed_groups():
    o = np.full(60, 100.0); c = np.full(60, 100.0); c[40] = 101.0
    assert C.directions(o, c, 40, 30) == (True, True)
    c2 = c.copy(); c2[40] = 99.0
    assert C.directions(o, c2, 40, 30) == (False, False)          # pump closed lower; below the story-minute open
    assert C.directions(o, c, 40, None) == (True, False)
    assert C.active("VOL_RAW", 40, None, 3.5, False, False) and not C.active("VOL", 40, None, 3.5, False, False)
    assert C.active("NEWS", 40, 30, None, False, True) and not C.active("NEWS", 40, 30, None, True, False)
    assert C.active("BOTH", 40, 30, 3.5, True, True) and not C.active("BOTH", 40, 30, 3.5, False, True)
