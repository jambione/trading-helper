"""Pure logic of the sector lead-lag study. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import sector_laglead_study as L  # noqa: E402


def _ser(start, closes):
    return {start + i: (c, c) for i, c in enumerate(closes)}


def test_universe_one_sector_per_name_and_frozen_digest():
    u = L.universe()
    assert len(u) >= 150 and u["NVDA"] == "SMH" and u["AAPL"] == "XLK" and u["GOOGL"] == "XLC"
    assert len(L.holdings_digest()) == 12


def test_close_at_carries_the_last_print_within_15_minutes():
    s = {600: (1.0, 10.0)}
    assert L.close_at(s, 610) == 10.0
    assert L.close_at(s, 616) is None


def test_sector_move_needs_absolute_and_relative_strength():
    m = 620
    etf = _ser(m - 5, [100.0, 100.1, 100.2, 100.2, 100.25, 100.3])    # +0.30%
    flat_spy = _ser(m - 5, [500.0] * 6)
    assert L.sector_moves(etf, flat_spy, m)
    assert not L.sector_moves(_ser(m - 5, [100.0] * 5 + [100.2]), flat_spy, m)          # +0.20% < 0.25%
    assert not L.sector_moves(etf, _ser(m - 5, [500.0] * 5 + [501.5]), m)               # rel +0.0%
    assert L.sector_moves(etf, _ser(m - 5, [500.0] * 5 + [501.0]), m)                   # rel exactly +0.10%


def test_laggard_band():
    m = 620
    assert L.is_laggard(_ser(m - 5, [50.0] * 6), m)
    assert not L.is_laggard(_ser(m - 5, [50.0] * 5 + [50.05]), m)
    assert not L.is_laggard(_ser(m - 5, [50.0] * 5 + [49.70]), m)


def _rising_etf(lo, hi):
    return {m: (100.0, 100.0 * (1 + 0.0006 * (m - lo))) for m in range(lo, hi)}


def test_fires_episodes_and_refractory():
    etf, spy = _rising_etf(580, 700), {m: (500.0, 500.0) for m in range(580, 700)}
    members = {"AAA": {m: (20.0, 20.0) for m in range(580, 700)}, "BBB": {m: (30.0, 30.0) for m in range(580, 700)}}
    got = L.fires(list(range(585, 700)), etf, spy, members)
    by = {}
    for s, m, ep in got:
        by.setdefault(s, []).append(m)
    assert set(by) == {"AAA", "BBB"}
    assert all(b - a >= L.REFRACT for ms in by.values() for a, b in zip(ms, ms[1:]))
    assert len({ep for _s, _m, ep in got}) == 1                          # one continuous ETF move = one episode


def test_control_minute_needs_laggard_without_sector_move_and_distance():
    flat_etf = {m: (100.0, 100.0) for m in range(590, 700)}
    spy = {m: (500.0, 500.0) for m in range(590, 700)}
    member = {m: (20.0, 20.0) for m in range(590, 700)}
    sip = {m: (20.0, 20.0) for m in range(590, 720)}
    k = L.control_minute("AAA", "d", 630, member, flat_etf, spy, sip, [630])
    assert k is not None and abs(k - 630) > L.CTRL_EXCL and abs(k - 630) <= 60      # any side, +-60 min
    assert k == L.control_minute("AAA", "d", 630, member, flat_etf, spy, sip, [630])      # seeded
    assert L.control_minute("AAA", "d", 630, member, flat_etf, spy, sip, [600, 630, 660]) is None
    hot = _rising_etf(560, 700)
    assert L.control_minute("AAA", "d", 605, member, hot, spy, sip, [605]) is None          # sector moving all hour


def test_adv_and_verdict():
    rows = [(f"2026-09-{i:02d}", 20.0, 2e6) for i in range(1, 22)]
    assert L.adv_ok(rows, "2026-09-30") is True
    assert L.adv_ok(rows[:19], "2026-09-30") is None
    ok = {"net10": 0.0002, "t10": 2.1, "net20": 0.0001, "lift": 0.0001, "lift_t": 2.0, "net_m2": 0.0001,
          "n": 100, "days": 10, "episodes": 20, "net_drop3": 0.0001, "net_dropetf": 0.0001}
    assert L.half_verdict(ok) == "PASS"
    for k, bad in (("net20", -0.0001), ("lift_t", 1.9), ("net_m2", -0.0001), ("net_drop3", -0.0001),
                   ("net_dropetf", -0.0001)):
        assert L.half_verdict(dict(ok, **{k: bad})) == "FAIL", k
    assert L.half_verdict(dict(ok, episodes=19)) == "UNDERPOWERED"
    assert L.half_verdict(dict(ok, ctrl_cover=0.79)) == "UNDERPOWERED"
