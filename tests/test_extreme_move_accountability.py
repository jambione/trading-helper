"""Extreme day-movers (≥100%) off the book must carry an explicit reason.

Silent price_cap / shortlist misses on momentum paths were the failure mode:
a +112% name could vanish from the shortlist with nothing in seed_drops or
admit_ledger. Operator ask: if it is not on the book, name why.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402

_ET = ZoneInfo("America/New_York")
# 09:00 ET Friday 2026-09-18 — before the 09:30 morning-flood window, so
# the open-seed cap under test is the normal one and not the flood bypass.
PRE_FLOOD_TS = datetime(2026, 9, 18, 9, 0, tzinfo=_ET).timestamp()


def test_extreme_move_pct_default():
    assert DEFAULT_CONFIG["ai_watch_extreme_move_pct"] == 100.0
    assert ew.extreme_move_pct({}) == 100.0
    assert ew.extreme_move_pct({"ai_watch_extreme_move_pct": 80}) == 80.0
    assert ew.extreme_move_pct({"ai_watch_extreme_move_pct": 0}) == 0.0


def test_big_mover_logs_price_cap(monkeypatch):
    ew._clear_seed_drops()
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "HOTX", "price": 150.0, "pct_change": 112.0, "rvol": 5.0},
        {"ticker": "OKAY", "price": 12.0, "pct_change": 55.0, "rvol": 3.0},
    ])
    rows = ew._big_mover_from_dashboard(max_price=100.0, min_pct=50.0)
    syms = {r["symbol"] for _, r in rows}
    assert "OKAY" in syms
    assert "HOTX" not in syms
    drops = ew.seed_drop_snapshot()
    assert drops["counts"].get("momentum", {}).get("price_cap", 0) >= 1
    samples = drops["samples"].get("momentum") or []
    hot = [s for s in samples if s.get("symbol") == "HOTX"]
    assert hot and hot[0].get("reason") == "price_cap"
    assert hot[0].get("pct") == 112.0


def test_audit_extreme_desk_movers_classifies_leftovers(monkeypatch):
    ew._clear_seed_drops()
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "NVDA", "price": 180.0, "pct_change": 101.0, "rvol": 2.5},
        {"ticker": "SEAT", "price": 8.0, "pct_change": 105.0, "rvol": 4.0},
    ])
    ew._audit_extreme_desk_movers(
        {"ai_watch_extreme_move_pct": 100.0, "ai_watch_min_rvol": 2.0,
         "ai_watch_hot_move_rvol_waive_pct": 20.0},
        seated={"SEAT"},
        max_price=100.0,
    )
    drops = ew.seed_drop_snapshot()
    assert drops["counts"].get("momentum", {}).get("price_cap", 0) >= 1
    samples = drops["samples"].get("momentum") or []
    assert any(s.get("symbol") == "NVDA" and s.get("reason") == "price_cap"
               for s in samples)
    # Seated extreme name must not be logged as off-book.
    assert not any(s.get("symbol") == "SEAT" for s in samples)


def test_extreme_off_book_rows_from_drops_and_rejects():
    ew._clear_seed_drops()
    ew._note_seed_drop("momentum", "ABC", "price_cap", pct=120.0, price=140.0)
    ew._note_seed_drop("momentum", "LOW", "thin_rvol", pct=12.0, rvol=0.5)
    out = ew.extreme_off_book_rows(
        kept_symbols=[],
        rejected=[{"symbol": "XYZ", "reason": "no_tape", "pct_change": 130.0,
                   "source": "movers"}],
        cfg={"ai_watch_extreme_move_pct": 100.0},
        limit=12,
    )
    syms = {r["symbol"]: r for r in out}
    assert "ABC" in syms and syms["ABC"]["reason"] == "price_cap"
    assert "XYZ" in syms and syms["XYZ"]["stage"] == "inclusion"
    assert "LOW" not in syms  # below floor


def test_write_admit_funnel_includes_extreme_off_book(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "REPORT_DIR", tmp_path)
    ew._clear_seed_drops()
    ew._note_seed_drop("momentum", "HOTX", "price_cap", pct=112.0, price=150.0)
    monkeypatch.setattr("ai_positions.log_event", lambda *a, **k: None)
    out = ew.write_admit_funnel(
        candidates=[{"symbol": "AOUT", "source": "movers"}],
        kept=[{"symbol": "AOUT", "source": "movers"}],
        rejected=[],
        now=1_000_000.0,
    )
    data = json.loads((tmp_path / "admit_funnel.json").read_text())
    assert data.get("extreme_move_pct") == 100.0
    extreme = data.get("extreme_off_book") or []
    assert any(r.get("symbol") == "HOTX" and r.get("reason") == "price_cap"
               for r in extreme)
    assert out.get("extreme_off_book") is not None


def test_public_snapshot_publishes_pct_change(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "_desk_pct_change", lambda s: 42.5 if s == "ZZZ" else None)
    monkeypatch.setattr(ew, "live_print", lambda sym: None)
    state = {
        "ZZZ": {
            "symbol": "ZZZ",
            "status": "watching",
            "score": 1.0,
            "last_ask": 10.0,
            "last_ask_src": "rest",
            "last_ask_age_sec": 1.0,
            "admit_pct_change": 10.0,
            "structure": {
                "wait_kind": "wait_for_zone",
                "entry_low": 9.0,
                "entry_high": 11.0,
                "stop_price": 8.5,
            },
        },
    }
    snap = ew.public_snapshot(state)
    assert len(snap) == 1
    assert snap[0]["pct_change"] == 42.5
    assert snap[0]["admit_pct_change"] == 10.0


def test_mom_open_shortlist_cap_logs_extreme(monkeypatch):
    ew._clear_seed_drops()
    # Many extreme names; soft-open N=1 so the rest hit shortlist_cap.
    desk = []
    for i in range(5):
        desk.append({
            "ticker": f"H{i}",
            "price": 5.0 + i,
            "pct_change": 110.0 + i,
            "rvol": 5.0,
            "price_age_sec": 1.0,
        })
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: desk)
    monkeypatch.setattr(ew, "_momentum_flagged_from_dashboard", lambda mp: [])
    monkeypatch.setattr(ew, "_big_mover_from_dashboard", lambda mp, mn: [])
    monkeypatch.setattr(ew, "research_candidate_rows", lambda: [])
    rows = ew.desk_candidate_rows({
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_momentum_open": True,
        "ai_watch_seed_momentum_open_n": 1,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": False,
        "ai_watch_seed_movers": False,
        "ai_watch_open_seed_min_pct": 0.0,
        "ai_watch_extreme_move_pct": 100.0,
        "ai_max_price": 100.0,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_hot_move_rvol_waive_pct": 20.0,
    }, PRE_FLOOD_TS)
    assert len(rows) == 1
    drops = ew.seed_drop_snapshot()
    # Truncated extremes should be named shortlist_cap (or shortlist_miss via audit).
    reasons = drops["counts"].get("momentum") or {}
    assert (reasons.get("shortlist_cap", 0) + reasons.get("shortlist_miss", 0)) >= 1
