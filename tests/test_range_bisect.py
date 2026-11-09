"""Lower-half entries use the live range position, not a recomputed high and low."""
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
import range_bisect as rb  # noqa: E402

T0 = 1_793_800_000.0


def _bars(prices, end=T0 - 90):
    n = len(prices)
    return [(end - 60 * (n - 1 - i), p, p + 0.02, p - 0.02, p, 1000.0) for i, p in enumerate(prices)]


def _feat(pos, rng=40.0):
    return {"rs_rng15_bp": rng, "rs_rise15_bp": 5.0, "rs_er15": 0.10,
            "wr_d10_slow": -1.0, "wr_span": 20.0, "wr_net": 5.0, "rs_pos15": pos}


def test_position_matches_the_live_function():
    prices = [50.0 + 0.01 * ((i * 7) % 13) for i in range(300)]
    bars = _bars(prices)
    out = fc.live_features(bars, 50.05, T0)
    rs = ew._range_shape_fields("X", 50.05, T0, fc.CFG)
    assert out["rs_pos15"] == pytest.approx(rs["rs_pos15"])


def test_entry_is_the_lower_half_of_a_sideways_range():
    assert rb.is_lower(_feat(0.2)) is True
    assert rb.is_lower(_feat(0.5)) is True
    assert rb.is_upper(_feat(0.5)) is False
    assert rb.is_lower(_feat(0.8)) is False
    assert rb.is_upper(_feat(0.8)) is True
    assert rb.is_lower(_feat(0.2, rng=8.0)) is False

    wave = [100.0 + math.sin(2 * math.pi * i / 10) for i in range(300)]
    bars = _bars(wave)
    last = wave[-15:]
    at_low = fc.live_features(bars, min(last), T0)
    at_high = fc.live_features(bars, max(last), T0)
    assert at_low["rs_pos15"] == pytest.approx(0.0, abs=0.05)
    assert rb.is_lower(at_low) is True
    assert rb.is_lower(at_high) is False


def test_planted_lower_half_separates_and_a_null_does_not():
    days = [f"2026-01-{d:02d}" for d in range(1, 41)]
    rng = random.Random(1)
    rows = []
    for d in days:
        for k in range(30):
            lower = k < 10
            rows.append({"d": d, "sideways": k < 20, "rs_pos15": 0.2 if lower else 0.8,
                         "gross": rng.uniform(-5, 5) + (8 if lower else 0), "ng": False})
    good = rb.summarize(rb.mark(rows), days)
    assert good["verdict"] in ("EDGE", "PROFITABLE")
    assert good["day_t"]["A"] >= fc.T_BAR and good["day_t"]["B"] >= fc.T_BAR
    rng = random.Random(2)
    nulls = [{"d": d, "sideways": k < 20, "rs_pos15": 0.2 if k < 10 else 0.8,
              "gross": rng.uniform(-5, 5), "ng": False} for d in days for k in range(30)]
    assert rb.summarize(rb.mark(nulls), days)["verdict"] != "PROFITABLE"


def test_underpowered_cannot_pass():
    rows = [{"d": "2026-06-01", "sideways": True, "rs_pos15": 0.2 if i < 3 else 0.8,
             "gross": 10.0, "ng": False} for i in range(6)]
    got = rb.summarize(rb.mark(rows), ["2026-06-01", "2026-06-02"])
    assert got["verdict"] == "UNDERPOWERED"
