import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import dip_anatomy as da  # noqa: E402


def _bars(c, rng_pct=0.001):
    """o[t] = c[t-1]; an up bar's low sits at its open (a higher low than the prior down bar's), others wick down."""
    o = np.concatenate(([c[0]], c[:-1]))
    h = np.maximum(o, c) * (1 + rng_pct / 2)
    l = np.where(c > o, o, np.minimum(o, c) * (1 - rng_pct / 2))
    return o, h, l, c, np.full(len(c), 1000.0)


def _long_dip():
    c = np.full(390, 50.0)
    c[:100] = 50.0 + np.sin(np.arange(100)) * 0.005     # quiet: sigma ~ 0.1%
    c[100] = 51.0                                          # dip start (30-bar high)
    c[101:145] = 51.0 - 0.01 * np.arange(1, 45)            # 44 bars down
    c[145] = c[144] + 0.02                                 # first up close with a higher low
    c[146:] = c[145]
    return c


def test_long_dip_age_is_uncapped_and_turn_fires_once():
    o, h, l, c, v = _bars(_long_dip())
    ev, opened = da.detect(o, h, l, c, v, 45.0)
    assert 100 in opened
    turns = [e for e in ev if e["s0"] == 100]
    assert len(turns) == 1 and turns[0]["t"] == 145 and turns[0]["AGE"] == 45.0
    assert turns[0]["DEPTH"] > da.DEPTH_MIN


def test_turn_before_depth_floor_does_not_count():
    c = _long_dip()
    c[103] = c[102] + 0.005          # an early small turn while D < 2
    c[104:145] = c[103] - 0.01 * np.arange(1, 42)
    c[145] = c[144] + 0.02
    c[146:] = c[145]
    o, h, l, c, v = _bars(c)
    ev, _ = da.detect(o, h, l, c, v, 45.0)
    assert [e["t"] for e in ev if e["s0"] == 100] == [145]


def test_dip_ends_at_new_high():
    c = _long_dip()
    c[120] = 51.5                    # recovers above the start high before reaching depth -> new dip opens there
    c[121:] = 51.5
    o, h, l, c, v = _bars(c)
    ev, opened = da.detect(o, h, l, c, v, 45.0)
    assert not [e for e in ev if e["s0"] == 100] and 120 in opened


def test_features_need_no_future_bars():
    o, h, l, c, v = _bars(_long_dip())
    full, _ = da.detect(o, h, l, c, v, 45.0)
    for e in full:
        t = e["t"]
        cut, _ = da.detect(o[: t + 1], h[: t + 1], l[: t + 1], c[: t + 1], v[: t + 1], 45.0)
        same = [x for x in cut if x["t"] == t]
        assert same and all(same[0][k] == e[k] or (np.isnan(same[0][k]) and np.isnan(e[k])) for k in ("AGE", "DEPTH", "SPEED", "VOLUME"))


def test_entry_exit_rules():
    o = np.full(40, 10.0)
    c = np.full(40, 10.0)
    c[16] = 11.0
    assert abs(da.entry_exit(o, c, 1) - 1000.0) < 1e-9          # buy open t+1, sell close t+15
    o2 = o.copy()
    o2[2] = np.nan
    assert abs(da.entry_exit(o2, c, 1) - 1000.0) < 1e-9          # entry slides to t+2
    o2[3] = np.nan
    assert np.isnan(da.entry_exit(o2, c, 1))                     # no open within 2 minutes -> dropped


def _synth_day(seed, U=60):
    rng = np.random.default_rng(seed)
    C = 50 * np.exp(np.cumsum(rng.normal(0, 0.0015, (U, 390)), axis=1))
    O = np.concatenate([C[:, :1], C[:, :-1]], axis=1)
    H = np.maximum(O, C) * (1 + np.abs(rng.normal(0, 0.0008, (U, 390))))
    L = np.minimum(O, C) * (1 - np.abs(rng.normal(0, 0.0008, (U, 390))))
    V = rng.uniform(500, 1500, (U, 390))
    return {"syms": [f"S{k}" for k in range(U)], "o": O, "h": H, "l": L, "c": C, "v": V,
            "pc": C[:, 0] / 1.03, "spy_c": 400 * np.exp(np.cumsum(rng.normal(0, 0.0005, 390)))}


def test_null_canary_no_edge_on_a_random_walk():
    rows, counts = [], defaultdict(int)
    for i in range(24):
        day = f"2026-0{5 if i < 12 else 8}-{i % 12 + 1:02d}"
        rows += [r for r in da.day_events(day, _synth_day(i), counts) if "blind" not in r]
    da.add_lifts(rows)
    st = da.edge_cell(rows)
    assert counts["events"] > 100
    assert st["verdict"] != "EDGE", st
    cuts = da.cut_points(rows)
    assert da.canary(rows, cuts)
