"""The drift screen must not hand out a PASS the other screens would refuse.

Its whole job is to say whether an entry gate *could* exist on a universe.
A screen that calls a driftless walk "DRIFT" would send the desk hunting for
a gate that cannot be there, which is the failure mode that cost 2026-08.
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

drift_screen = pytest.importorskip("drift_screen")


def _rows(n, sessions, mfe, mae, net=0.0):
    return [{"day": f"2026-08-{1 + (i % sessions):02d}",
             "mfe": mfe, "mae": mae, "net": net}
            for i in range(n)]


def test_symmetric_excursion_is_not_drift():
    """MFE == MAE is the null. It must never read as DRIFT."""
    s = drift_screen.score(_rows(200, 10, 1.0, 1.0))
    assert s["verdict"] == "NO_DRIFT"
    assert s["mfe_over_mae"] == pytest.approx(1.0)


def test_adverse_heavy_universe_is_no_drift():
    """August's watchlist: MFE/MAE below 1. Explicitly not a gate candidate."""
    s = drift_screen.score(_rows(200, 10, 0.75, 1.0, net=-0.05))
    assert s["verdict"] == "NO_DRIFT"
    assert s["mfe_over_mae"] < 1.0


def test_clean_drift_passes_every_gate():
    random.seed(7)
    rows = []
    for i in range(200):
        rows.append({"day": f"2026-08-{1 + (i % 10):02d}",
                     "mfe": 1.5 + random.gauss(0, 0.1),
                     "mae": 0.5 + random.gauss(0, 0.1),
                     "net": 0.4 + random.gauss(0, 0.1)})
    s = drift_screen.score(rows)
    assert s["verdict"] == "DRIFT", s["why"]
    assert s["sigma"] >= drift_screen.MIN_SIGMA


def test_one_session_cannot_carry_the_sample():
    """90% of n from a single day is the max-day-share failure, not a PASS."""
    rows = _rows(20, 5, 1.5, 0.5, net=0.3)
    rows += [{"day": "2026-08-01", "mfe": 1.5, "mae": 0.5, "net": 0.3}
             for _ in range(180)]
    s = drift_screen.score(rows)
    assert s["verdict"] != "DRIFT"
    assert "of n" in s["why"]


def test_too_few_sessions_is_underpowered_not_pass():
    s = drift_screen.score(_rows(100, 2, 1.5, 0.5, net=0.3))
    assert s["verdict"] == "UNDERPOWERED"


def test_empty_is_not_a_verdict():
    assert drift_screen.score([])["verdict"] == "EMPTY"


def test_sign_p_matches_binomial():
    assert drift_screen._sign_p(10, 10) == pytest.approx(1 / 1024)
    assert drift_screen._sign_p(5, 10) == pytest.approx(638 / 1024)
    assert drift_screen._sign_p(0, 0) == 1.0


def _bar(t, o, h, low, c, day="2026-08-20", hm=(14, 0)):
    return {"t": t, "day": day, "hm": hm, "o": o, "h": h, "l": low, "c": c}


def test_samples_do_not_overlap_at_default_stride():
    bars = [_bar(i * 60, 10, 10.1, 9.9, 10) for i in range(30)]
    out = drift_screen.sample_excursions(
        bars, "2026-08-20", horizon=10, stride=10, after_ts=0.0, rth_only=True)
    assert len(out) == 3


def test_eligible_within_drops_bars_before_admit():
    bars = [_bar(i * 60, 10, 10.1, 9.9, 10) for i in range(30)]
    out = drift_screen.sample_excursions(
        bars, "2026-08-20", horizon=10, stride=10,
        after_ts=20 * 60, rth_only=True)
    assert len(out) == 1


def test_premarket_excluded_by_default():
    early = [_bar(i * 60, 10, 10.1, 9.9, 10, hm=(9, 0)) for i in range(20)]
    out = drift_screen.sample_excursions(
        early, "2026-08-20", horizon=5, stride=5, after_ts=0.0, rth_only=True)
    assert out == []
    out = drift_screen.sample_excursions(
        early, "2026-08-20", horizon=5, stride=5, after_ts=0.0, rth_only=False)
    assert len(out) == 4


def test_excursions_are_measured_from_the_entry_open():
    bars = [_bar(0, 100.0, 102.0, 99.0, 101.0),
            _bar(60, 101.0, 103.0, 98.0, 99.0)]
    out = drift_screen.sample_excursions(
        bars, "2026-08-20", horizon=2, stride=2, after_ts=0.0, rth_only=True)
    assert len(out) == 1
    assert out[0]["mfe"] == pytest.approx(3.0)   # 103 vs 100
    assert out[0]["mae"] == pytest.approx(2.0)   # 100 vs 98
    assert out[0]["net"] == pytest.approx(-1.0)  # close 99 vs 100


def test_symbol_cap_does_not_let_the_alphabet_pick_the_sample():
    """sorted()[:limit] cut a 314-name universe at KTOS and dropped 52%."""
    syms = [f"{a}{b}" for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "AB"]
    picked = drift_screen.select_symbols(syms, 20)
    assert len(picked) == 20
    assert picked != sorted(syms)[:20]
    # a late-alphabet name must be reachable at all
    assert any(s[0] > "M" for s in picked)


def test_symbol_cap_is_deterministic():
    syms = [f"SYM{i:03d}" for i in range(300)]
    assert drift_screen.select_symbols(syms, 50) == \
        drift_screen.select_symbols(syms, 50)


def test_no_cap_returns_everything_deduped():
    syms = ["B", "A", "B", "C"]
    assert drift_screen.select_symbols(syms, 0) == ["A", "B", "C"]
    assert drift_screen.select_symbols(syms, 99) == ["A", "B", "C"]
