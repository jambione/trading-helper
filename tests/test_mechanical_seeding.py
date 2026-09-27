"""Mechanical seeding hygiene (name-finding 9/27): cfg 0, book echo, bb_live band."""
from __future__ import annotations

import time

import pytest

import ai_entry_watch as ew


def test_cfg_float_keeps_explicit_zero():
    assert ew._cfg_float({"ai_watch_min_pct_change": 0.0}, "ai_watch_min_pct_change", 50.0) == 0.0
    assert ew._cfg_float({"ai_watch_min_pct_change": 0}, "ai_watch_min_pct_change", 50.0) == 0.0
    assert ew._cfg_float({}, "ai_watch_min_pct_change", 50.0) == 50.0
    assert ew._cfg_float({"ai_watch_min_pct_change": None}, "ai_watch_min_pct_change", 50.0) == 50.0


def test_big_mover_respects_zero_min_pct(monkeypatch):
    rows = [
        {"ticker": "AAA", "pct_change": 1.0, "price": 25.0, "src": "momentum"},
        {"ticker": "BBB", "pct_change": 60.0, "price": 25.0, "src": "momentum"},
        {"ticker": "CCC", "pct_change": 2.0, "price": 25.0, "src": "book"},
    ]
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: rows)
    # With min_pct=0, any positive day change qualifies (signed).
    got = ew._big_mover_from_dashboard(100.0, 0.0)
    syms = {r["symbol"] for _, r in got}
    assert "AAA" in syms
    assert "BBB" in syms
    assert "CCC" not in syms  # book echo skipped


def test_momentum_flagged_skips_book_echo(monkeypatch):
    rows = [
        {"ticker": "ECHO", "pct_change": 12.0, "price": 30.0, "src": "book",
         "find_it_first": True},
        {"ticker": "REAL", "pct_change": 12.0, "price": 30.0, "src": "momentum",
         "find_it_first": True},
    ]
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: rows)
    got = ew._momentum_flagged_from_dashboard(100.0)
    syms = {r["symbol"] for _, r in got}
    assert syms == {"REAL"}


def test_seed_rvol_premarket_near_zero_abstains(monkeypatch):
    # 08:00 ET on a fixed day
    import datetime as dt
    from zoneinfo import ZoneInfo
    et = dt.datetime(2026, 9, 25, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    now = et.timestamp()
    assert ew._seed_rvol_value(0.0, now=now) is None
    assert ew._seed_rvol_value(0.01, now=now) is None
    assert ew._seed_rvol_value(1.5, now=now) == 1.5
    # After open, 0.01 is known-thin (not abstained here — gate decides)
    et2 = dt.datetime(2026, 9, 25, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert ew._seed_rvol_value(0.01, now=et2.timestamp()) == 0.01


def test_min_price_for_bb_live_override():
    cfg = {
        "ai_watch_min_price": 20.0,
        "ai_watch_bb_live_min_price": 2.0,
        "ai_watch_momentum_min_price": 20.0,
    }
    assert ew._min_price_for("bb_live", cfg) == 2.0
    assert ew._min_price_for("bro", cfg) == 2.0
    assert ew._min_price_for("momentum", cfg) == 20.0
    assert ew._min_price_for("movers", cfg) == 20.0
    cfg2 = {"ai_watch_min_price": 20.0, "ai_watch_bb_live_min_price": None}
    assert ew._min_price_for("bb_live", cfg2) == 20.0


def test_spread_gate_bb_live_exempt():
    cfg = {"ai_watch_max_sip_spread_pct": 0.2, "ai_watch_bb_live_spread_exempt": True}
    assert ew._spread_gate_max("bb_live", cfg) == 0.0
    assert ew._spread_gate_max("movers", cfg) == 0.2
