"""Hot-move RVOL waive: seat +20% movers that thin_rvol used to wipe.

2026-09-04 midday: movers panel had BIAF +52% / LABX +23% / CBRG +23% with
SIP rvol 0.8–1.8x against ai_watch_min_rvol=2.0 — seed shortlist held ~0
movers while the scan panel was full. Unknown rvol still abstains; known-thin
below the floor still refuses unless day-chg clears the waive.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402


def test_hot_move_waive_knobs_default():
    assert DEFAULT_CONFIG["ai_watch_movers_min_rvol"] == 1.0
    assert DEFAULT_CONFIG["ai_watch_hot_move_rvol_waive_pct"] == 20.0
    assert DEFAULT_CONFIG["ai_watch_seed_movers_n"] == 16


def test_rvol_blocks_admit_waives_hot_movers():
    cfg = {
        "ai_watch_movers_min_rvol": 1.0,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_hot_move_rvol_waive_pct": 20.0,
    }
    # BIAF-class: known-thin vs movers floor, but +52% waives.
    assert ew.rvol_blocks_admit(0.80, 52.0, cfg, source="movers") is None
    # LABX-class: 1.47 < general 2.0 but >= movers 1.0 — pass without waive.
    assert ew.rvol_blocks_admit(1.47, 23.0, cfg, source="movers") is None
    # Quiet thin name: no waive.
    assert ew.rvol_blocks_admit(0.80, 5.0, cfg, source="movers") == "thin_rvol"
    # Unknown abstains.
    assert ew.rvol_blocks_admit(None, 5.0, cfg, source="movers") is None
    # Momentum uses general floor; hot waive still applies.
    assert ew.rvol_blocks_admit(0.5, 25.0, cfg, source="momentum") is None
    assert ew.rvol_blocks_admit(0.5, 10.0, cfg, source="momentum") == "thin_rvol"


def test_passes_inclusion_admits_hot_thin_mover(monkeypatch):
    monkeypatch.setattr("float_feed.float_shares", lambda s: 5.0, raising=False)
    cfg = {
        "ai_watch_require_uptrend": True,
        "ai_watch_min_price": 2.0,
        "ai_min_dollar_volume": 0.0,
        "ai_watch_max_float_m": 0,
        "ai_watch_require_indicators": False,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_movers_min_rvol": 1.0,
        "ai_watch_hot_move_rvol_waive_pct": 20.0,
    }
    ok, met, why = ew.passes_inclusion(
        {"symbol": "BIAF", "source": "movers", "price": 19.4,
         "pct_change": 52.0, "rvol": 0.80, "criteria": []},
        cfg,
    )
    assert ok is True and why == ""
    assert "hot_move_rvol_waive" in met

    ok, met, why = ew.passes_inclusion(
        {"symbol": "THIN", "source": "movers", "price": 5.0,
         "pct_change": 8.0, "rvol": 0.80, "criteria": []},
        cfg,
    )
    assert ok is False and why == "thin_rvol"


def test_movers_seed_seats_hot_thin_names(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(ew, "_live_quote_map", lambda: ({}, {}))
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "research_candidate_rows", lambda: [])
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": __import__("time").time(),
        "rows": [
            {"symbol": "BIAF", "price": 19.4, "pct_change": 52.0, "rvol": 0.80},
            {"symbol": "LABX", "price": 12.6, "pct_change": 23.7, "rvol": 1.47},
            # +12% clears movers pct floor but not the 20% hot waive → thin_rvol.
            {"symbol": "QUIET", "price": 5.0, "pct_change": 12.0, "rvol": 0.50},
            {"symbol": "AOUT", "price": 14.0, "pct_change": 41.0, "rvol": 59.0},
        ],
    }), encoding="utf-8")
    (tmp_path / "trending_stocks.json").write_text(
        json.dumps({"rows": []}), encoding="utf-8")

    rows = ew.desk_candidate_rows({
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_momentum_open": False,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": False,
        "ai_watch_seed_movers": True,
        "ai_watch_seed_movers_n": 16,
        "ai_watch_movers_min_pct_change": 10.0,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_movers_min_rvol": 1.0,
        "ai_watch_hot_move_rvol_waive_pct": 20.0,
        "ai_max_price": 100.0,
        "ai_movers_max_age_sec": 900.0,
        "ai_watch_movers_enrich": False,
    })
    syms = {r["symbol"] for r in rows}
    assert "BIAF" in syms
    assert "LABX" in syms
    assert "AOUT" in syms
    assert "QUIET" not in syms
    drops = ew.seed_drop_snapshot()
    assert drops["counts"].get("movers", {}).get("thin_rvol", 0) >= 1


def test_write_admit_funnel_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "REPORT_DIR", tmp_path)
    ew._clear_seed_drops()
    ew._note_seed_drop("movers", "BIAF", "thin_rvol", pct=52.0, rvol=0.8)
    out = ew.write_admit_funnel(
        candidates=[{"symbol": "AOUT", "source": "movers"}],
        kept=[{"symbol": "AOUT", "source": "movers"}],
        rejected=[{"symbol": "PATH", "reason": "not_uptrend"}],
        now=1_000_000.0,
    )
    path = tmp_path / "admit_funnel.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["n_kept"] == 1
    assert data["inclusion_reject_reasons"]["not_uptrend"] == 1
    assert data["seed_drops"]["counts"]["movers"]["thin_rvol"] == 1
    assert out["kept_symbols"] == ["AOUT"]
