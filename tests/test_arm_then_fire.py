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
    assert af.arm_state([H(T - 600, "Movers: 10 stocks")], T)[0] is None  # any skipped headline: not UNARMED
    assert af.arm_state([H(T - 7200, "Q3 earnings beat")], T) == ("W2", True)
    assert af.arm_state([H(T - 20 * 3600, "Q3 results"), H(T - 600)], T) == ("W1", True)   # earnings anywhere in window
    assert af.arm_state([H(T - 80 * 3600)], T, lookback=90 * 3600)[0] is None              # prior-session lookback


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


def test_fires_need_no_future_bars():
    from stack_search import layers
    rng = np.random.default_rng(5)
    def walk(px):
        c = px * np.exp(np.cumsum(rng.normal(0, 0.002, 390)))
        o = np.concatenate(([c[0]], c[:-1]))
        return o, np.maximum(o, c) * 1.001, np.minimum(o, c) * 0.999, c, rng.uniform(500, 3000, 390)
    po, ph, pl, pc, pv = walk(50)
    o, h, l, c, v = walk(pc[-1])
    v = v * 3
    spy = 400 * np.exp(np.cumsum(rng.normal(0, 0.0005, 390)))
    lay, chg = layers(o, h, l, c, v, pc, ph, pl, pv, spy)
    G, px = np.zeros(390), np.ones(390, bool)
    full = af.fires_for_name(lay, chg, G, px)
    for t in (60, 200, 340):
        cut = lambda a: np.concatenate([a[: t + 1], np.full(389 - t, np.nan)])
        lay2, chg2 = layers(cut(o), cut(h), cut(l), cut(c), cut(v), pc, ph, pl, pv, cut(spy))
        part = af.fires_for_name(lay2, chg2, G, px)
        assert [f for f in part if f[1] <= t] == [f for f in full if f[1] <= t]


def test_market_split_buckets():
    rows = [{"day": "d1", "lift": 10.0, "w": 1.0}, {"day": "d2", "lift": -5.0, "w": 1.0}, {"day": "d3", "lift": 2.0, "w": 1.0}]
    out = af.split_by_market(rows, {"d1": -0.02, "d2": 0.0, "d3": 0.015})
    assert out["down<=-1%"]["n"] == 1 and out["flat"]["n"] == 1 and out["up>=+1%"]["n"] == 1
