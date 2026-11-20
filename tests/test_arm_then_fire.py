import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import arm_then_fire as af  # noqa: E402


def H(t, text="Company wins contract", n=1, upd=None):
    return {"t": t, "headline": text, "n_symbols": n, "updated": upd}


def test_classify():
    assert af.classify(H(0)) == "qualify"
    assert af.classify(H(0, "Movers: 10 stocks", 1)) == "skip"
    assert af.classify(H(0, "Why XYZ shares are trading higher", 1)) == "skip"
    assert af.classify(H(0, "x", 6)) == "skip"
    assert af.classify(H(0, "x", 1, upd=200)) == "skip"
    assert af.classify(H(0, "Prices registered direct offering")) == "bearish"
    assert af.classify(H(0, "atm the bell")) == "qualify"


def test_arm_windows_latency_and_exclusions():
    T = 100_000.0
    assert af.arm_state([H(T - 600)], T)[0] == "W1"
    assert af.arm_state([H(T - 60)], T)[0] is None            # < 2 minutes before the fire bar
    assert af.arm_state([H(T + 10)], T)[0] == "UNARMED"       # after the fire bar open: ignored
    assert af.arm_state([H(T - 2 * 3600)], T)[0] == "W2"
    assert af.arm_state([H(T - 10 * 3600)], T)[0] == "W3"
    assert af.arm_state([H(T - 40 * 3600)], T)[0] is None     # 30-72 h excluded
    assert af.arm_state([H(T - 80 * 3600)], T)[0] == "UNARMED"
    assert af.arm_state([H(T - 600, "offering priced")], T)[0] is None   # bearish only: neither armed nor unarmed
    assert af.arm_state([H(T - 7200, "Q3 earnings beat")], T) == ("W2", True)


def test_fires_first_cross_and_refractory():
    n = 390
    lay = {"M1": np.zeros(n, bool), "T1": np.zeros(n, bool), "T2": np.zeros(n, bool)}
    lay["M1"][[10, 50]] = True                     # first crossing before 09:45 -> no F1 fire
    lay["T2"][[20, 30, 60]] = True
    chg = np.zeros(n)
    chg[100:] = 0.05
    G = np.zeros(n)
    px = np.ones(n, bool)
    f = af.fires_for_name(lay, chg, G, px)
    assert ("F1", 10) not in f and not any(t == "F1" for t, _ in f)
    assert ("F4", 100) in f
    assert [x for x in f if x[0] == "F2"] == [("F2", 20), ("F2", 60)]


def test_lifts_match_trigger_vol_gap_and_exclude_self():
    fs = [{"day": "2026-05-01", "sym": "A", "trig": "F2", "t": 100, "g": 30.0, "vt": 1, "gb": 1, "arm": "W1", "earn": False},
          {"day": "2026-05-01", "sym": "B", "trig": "F2", "t": 110, "g": 10.0, "vt": 1, "gb": 1, "arm": "UNARMED", "earn": False},
          {"day": "2026-05-01", "sym": "C", "trig": "F2", "t": 105, "g": 50.0, "vt": 2, "gb": 1, "arm": "UNARMED", "earn": False},
          {"day": "2026-05-01", "sym": "D", "trig": "F3", "t": 100, "g": 90.0, "vt": 1, "gb": 1, "arm": "UNARMED", "earn": False}]
    rows, dropped, _, _ = af.lifts(fs, "W1", "F2")
    assert dropped == 0 and rows[0]["lift"] == 20.0 and rows[0]["w"] == 0.5
