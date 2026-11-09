"""The sideways study must call the desk's live functions, not a copy of them.

Sideways means traveling between a high and a low. A motionless tape is not sideways.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "studies"))
import ai_entry_watch as ew  # noqa: E402
import flat_conjunction as fc  # noqa: E402
from tools import sell_double_check as sdc  # noqa: E402

T0 = 1_793_800_000.0


def _bars(prices, end=T0 - 90):
    n = len(prices)
    return [(end - 60 * (n - 1 - i), p, p + 0.02, p - 0.02, p, 1000.0) for i, p in enumerate(prices)]


def _between() -> dict:
    """Hand-built live readings of a trip between a high and a low."""
    return {"rs_rng15_bp": 40.0, "rs_rise15_bp": 5.0, "rs_er15": 0.10,
            "wr_d10_slow": -1.0, "wr_span": 20.0, "wr_net": 5.0}


def test_features_come_from_the_live_functions(monkeypatch):
    seen = []

    def range_fields(sym, price, now, cfg):
        seen.append("range")
        return {"rs_rng15_bp": 40.0, "rs_rise15_bp": 5.0, "rs_er15": 0.10}

    def slope_fields(sym, price, now, cfg):
        seen.append("slope")
        return {"wr_d10_slow": -1.0}

    def window(rows, price, length, span, lookback):
        seen.append("window")
        return [-20.0, -40.0, -25.0]

    monkeypatch.setattr(ew, "_range_shape_fields", range_fields)
    monkeypatch.setattr(ew, "_wr_slope_fields", slope_fields)
    monkeypatch.setattr(fc.sdc, "line_window", window)
    out = fc.live_features(_bars([50.0] * 20), 50.0, T0)
    assert seen == ["range", "slope", "window"]
    assert out["rs_rng15_bp"] == 40.0 and out["wr_d10_slow"] == -1.0
    assert out["wr_span"] == 20.0 and out["wr_net"] == 5.0
    assert fc.is_sideways(out) is True


def test_numbers_match_a_direct_call():
    prices = [50.0 + 0.01 * ((i * 7) % 13) for i in range(300)]
    bars = _bars(prices)
    price = 50.05
    out = fc.live_features(bars, price, T0)
    direct = ew._wr_slope_fields("X", price, T0, fc.CFG)
    rs = ew._range_shape_fields("X", price, T0, fc.CFG)
    closed = sdc.closed_rows([b[0] for b in bars], [b[2] for b in bars], [b[3] for b in bars],
                             [b[4] for b in bars], T0)
    fw = sdc.line_window(closed, price, sdc.FAST_LENGTH, sdc.FAST_SPAN, 11)
    assert out is not None and direct and rs and fw
    assert out["wr_d10_slow"] == pytest.approx(direct["wr_d10_slow"])
    assert out["rs_rng15_bp"] == pytest.approx(rs["rs_rng15_bp"])
    assert out["rs_rise15_bp"] == pytest.approx(rs["rs_rise15_bp"])
    assert out["rs_er15"] == pytest.approx(rs["rs_er15"])
    assert out["wr_span"] == pytest.approx(max(fw) - min(fw))
    assert out["wr_net"] == pytest.approx(abs(fw[-1] - fw[0]))


def test_between_levels_is_sideways_and_a_rise_or_a_still_tape_is_not():
    assert fc.is_sideways(_between()) is True
    assert fc.is_sideways({**_between(), "rs_rng15_bp": 8.0}) is False
    assert fc.is_sideways({**_between(), "rs_er15": 1.0, "rs_rise15_bp": 40.0}) is False
    assert fc.is_sideways({**_between(), "wr_span": 10.0, "wr_net": 10.0}) is False
    assert fc.is_sideways({**_between(), "wr_d10_slow": 1.0}) is False

    rise = [40.0 + 0.05 * i for i in range(300)]
    rose = fc.live_features(_bars(rise), rise[-1], T0)
    assert rose["rs_er15"] > fc.ER_MAX
    assert fc.is_sideways(rose) is False

    still = fc.live_features(_bars([50.0 + 0.001 * (i % 2) for i in range(300)]), 50.0, T0)
    assert still["rs_rng15_bp"] < fc.RNG_MIN_BP
    assert fc.is_sideways(still) is False

    wave = [100.0 + math.sin(2 * math.pi * i / 10) for i in range(300)]
    swung = fc.live_features(_bars(wave), wave[-1], T0)
    assert swung["rs_rng15_bp"] >= fc.RNG_MIN_BP
    assert swung["rs_er15"] <= fc.ER_MAX
    assert fc.is_sideways(swung) is True


def test_planted_edge_separates_and_a_null_does_not():
    days = [f"2026-01-{d:02d}" for d in range(1, 41)]
    rng = random.Random(1)
    rows = []
    for d in days:
        for k in range(30):
            sideways = k < 10
            rows.append({"d": d, "sideways": sideways, "gross": rng.uniform(-5, 5) + (0 if sideways else 8),
                         "ng": False})
    good = fc.summarize(rows, days)
    assert good["verdict"] in ("EDGE", "PROFITABLE")
    assert good["day_t"]["A"] >= fc.T_BAR and good["day_t"]["B"] >= fc.T_BAR
    rng = random.Random(2)
    nulls = [{"d": d, "sideways": k < 10, "gross": rng.uniform(-5, 5), "ng": False}
             for d in days for k in range(30)]
    assert fc.summarize(nulls, days)["verdict"] != "PROFITABLE"


def test_underpowered_cannot_pass():
    rows = [{"d": "2026-06-01", "sideways": i < 3, "gross": 10.0, "ng": False} for i in range(10)]
    got = fc.summarize(rows, ["2026-06-01", "2026-06-02"])
    assert got["verdict"] == "UNDERPOWERED"
