"""Pure logic of the sector lead-lag study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import sector_laglead_study as L  # noqa: E402


def _ser(start, closes):
    return {start + i: (c, c) for i, c in enumerate(closes)}


def test_etf_for_uses_the_sector_study_map():
    assert L.etf_for("Semiconductors") == "SMH"
    assert L.etf_for("Banking") == "XLF"
    assert L.etf_for("Software") == "XLK"
    assert L.etf_for("") is None and L.etf_for(None) is None


def test_close_at_carries_the_last_print_within_15_minutes():
    s = {600: (1.0, 10.0)}
    assert L.close_at(s, 610) == 10.0
    assert L.close_at(s, 616) is None


def test_sector_move_needs_absolute_and_relative_strength():
    m = 620
    etf = _ser(m - 5, [100.0, 100.1, 100.2, 100.2, 100.25, 100.3])    # +0.30%
    flat_spy = _ser(m - 5, [500.0] * 6)
    hot_spy = _ser(m - 5, [500.0, 500, 500, 500, 500, 501.0])          # +0.20%: relative only +0.10% -> exactly at bar
    assert L.sector_moves(etf, flat_spy, m)
    assert not L.sector_moves(_ser(m - 5, [100.0] * 5 + [100.2]), flat_spy, m)   # +0.20% < 0.25%
    assert not L.sector_moves(etf, _ser(m - 5, [500.0] * 5 + [501.5]), m)         # market move, rel +0.0%
    assert L.sector_moves(etf, hot_spy, m) is True


def test_laggard_band():
    m = 620
    assert L.is_laggard(_ser(m - 5, [50.0] * 6), m)                     # flat
    assert not L.is_laggard(_ser(m - 5, [50.0] * 5 + [50.05]), m)       # +0.10% already moving
    assert not L.is_laggard(_ser(m - 5, [50.0] * 5 + [49.70]), m)       # -0.60% falling apart


def test_fires_refractory_and_window():
    etf = {m: (100.0 + 0.1 * (m % 7 == 0) * 0, 100.0 * (1 + 0.0006 * (m - 590))) for m in range(580, 700)}
    spy = {m: (500.0, 500.0) for m in range(580, 700)}
    members = {"AAA": {m: (20.0, 20.0) for m in range(580, 700)}}
    got = L.fires(list(range(585, 700)), etf, spy, members)
    ms = [m for _s, m in got]
    assert ms and ms[0] >= L.WIN_START
    assert all(b - a >= L.REFRACT for a, b in zip(ms, ms[1:]))


def test_adv_and_verdict():
    rows = [(f"2026-09-{i:02d}", 20.0, 2e6) for i in range(1, 22)]       # $40M/day, $20 close
    assert L.adv_ok(rows, "2026-09-30") == (True, 20.0)
    assert L.adv_ok(rows[:19], "2026-09-30") == (None, None)
    assert L.half_verdict(0.0002, 2.1, 0.0001, 100, 10) == "PASS"
    assert L.half_verdict(0.0002, 2.1, -0.0001, 100, 10) == "FAIL"
    assert L.half_verdict(0.0002, 2.1, 0.0001, 99, 10) == "UNDERPOWERED"
