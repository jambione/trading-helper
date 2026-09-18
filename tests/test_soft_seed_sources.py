"""Soft-seed source parity: trending + movers + momentum + research.

Soft-seed is a scout shortlist under a shared max. Sources compete on
scout score. Soft-seed must never call desk_candidate_rows (seed-drop
tallies stay intact).
"""
from __future__ import annotations

import json

import pytest

import ai_entry_watch as ew
from config import DEFAULT_CONFIG


def _base_cfg(**over):
    cfg = {
        "ai_watch_soft_seed_enabled": True,
        "ai_watch_soft_seed_interval_sec": 1.0,
        "ai_watch_soft_seed_trending": True,
        "ai_watch_soft_seed_movers": True,
        "ai_watch_soft_seed_momentum": True,
        "ai_watch_soft_seed_research": True,
        "ai_watch_soft_seed_max": 12,
        "ai_max_price": 100.0,
        "ai_watch_min_pct_change": 50.0,
        "ai_movers_max_age_sec": 0.0,  # accept any movers file age in tests
        # Source-parity tests exercise scout shortlist, not arm-ready keep.
        "ai_watch_admit_require_arm_ready": False,
    }
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _reset_soft_seed_clock():
    ew._SOFT_SEED_LAST_TS = 0.0
    yield
    ew._SOFT_SEED_LAST_TS = 0.0


def test_default_config_enables_momentum_and_research_soft_seed():
    assert DEFAULT_CONFIG["ai_watch_soft_seed_momentum"] is True
    assert DEFAULT_CONFIG["ai_watch_soft_seed_research"] is True
    assert DEFAULT_CONFIG["ai_watch_soft_seed_movers"] is True
    assert DEFAULT_CONFIG["ai_watch_soft_seed_trending"] is True


def test_soft_seed_source_rows_includes_all_four_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "trending_stocks.json").write_text(json.dumps({
        "rows": [{
            "symbol": "TREND1", "price": 10.0, "pct_change": 8.0,
            "trending_score": 20.0, "look_reason": "EXT", "rvol": 3.0,
            "vol_session": 500_000,
        }],
    }), encoding="utf-8")
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": 1e12,
        "rows": [{
            "symbol": "MOVE1", "price": 12.0, "pct_change": 15.0,
            "rvol": 4.0, "dollar_volume": 5_000_000,
        }],
    }), encoding="utf-8")
    (tmp_path / "grok_suggestions.json").write_text(json.dumps({
        "source": "xai",
        "rows": [{"symbol": "RES1", "score": 9.0, "reason": "thesis"}],
    }), encoding="utf-8")

    monkeypatch.setattr(
        ew, "_momentum_flagged_from_dashboard",
        lambda max_price=None: [(10.0, {
            "symbol": "MOM1",
            "score": 10.0,
            "reason": "momentum FIRST",
            "source": "momentum",
            "price": 11.0,
            "pct_change": 22.0,
            "rvol": 2.5,
            "criteria": ["flag"],
        })],
    )
    monkeypatch.setattr(
        ew, "_big_mover_from_dashboard",
        lambda max_price=None, min_pct=50.0: [],
    )
    # Empty desk list so mom_open does not first-claim RES1 before research.
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (
        {"RES1": {"price": 9.5, "pct_change": 4.0, "rvol": 2.0,
                  "day_vol": 1_000_000}},
        {},
    ))

    rows = ew._soft_seed_source_rows(_base_cfg())
    by = {r["symbol"]: r for r in rows}

    assert by["TREND1"]["source"] == "trending"
    assert "soft_seed" in by["TREND1"]["criteria"]
    assert by["MOVE1"]["source"] == "movers"
    assert by["MOM1"]["source"] == "momentum"
    assert "momentum" in by["MOM1"]["criteria"]
    assert by["RES1"]["source"] == "xai"
    assert "research" in by["RES1"]["criteria"]
    assert by["RES1"]["price"] == 9.5
    assert by["RES1"]["pct_change"] == 4.0


def test_soft_seed_knobs_disable_momentum_and_research_independently(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "trending_stocks.json").write_text(json.dumps({
        "rows": [{
            "symbol": "TREND1", "price": 10.0, "pct_change": 8.0,
            "trending_score": 20.0, "look_reason": "EXT",
        }],
    }), encoding="utf-8")
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": 1e12,
        "rows": [{
            "symbol": "MOVE1", "price": 12.0, "pct_change": 15.0,
            "dollar_volume": 5_000_000,
        }],
    }), encoding="utf-8")
    (tmp_path / "grok_suggestions.json").write_text(json.dumps({
        "source": "xai",
        "rows": [{"symbol": "RES1", "score": 9.0, "reason": "thesis"}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        ew, "_momentum_flagged_from_dashboard",
        lambda max_price=None: [(10.0, {
            "symbol": "MOM1", "score": 10.0, "source": "momentum",
            "price": 11.0, "pct_change": 22.0, "criteria": ["flag"],
        })],
    )
    monkeypatch.setattr(
        ew, "_big_mover_from_dashboard",
        lambda max_price=None, min_pct=50.0: [],
    )
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (
        {"RES1": {"price": 9.5, "pct_change": 4.0, "day_vol": 1e6}},
        {},
    ))

    rows = ew._soft_seed_source_rows(_base_cfg(
        ai_watch_soft_seed_momentum=False,
        ai_watch_soft_seed_research=False,
    ))
    syms = {r["symbol"] for r in rows}
    assert "TREND1" in syms
    assert "MOVE1" in syms
    assert "MOM1" not in syms
    assert "RES1" not in syms


def test_soft_seed_first_claim_within_batch(tmp_path, monkeypatch):
    """Trending owns a symbol before movers/momentum/research can re-claim it."""
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "trending_stocks.json").write_text(json.dumps({
        "rows": [{
            "symbol": "SAME", "price": 10.0, "pct_change": 8.0,
            "trending_score": 20.0, "look_reason": "EXT",
        }],
    }), encoding="utf-8")
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": 1e12,
        "rows": [{"symbol": "SAME", "price": 12.0, "pct_change": 15.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        ew, "_momentum_flagged_from_dashboard",
        lambda max_price=None: [(10.0, {
            "symbol": "SAME", "score": 10.0, "source": "momentum",
            "price": 11.0, "pct_change": 22.0, "criteria": ["flag"],
        })],
    )
    monkeypatch.setattr(
        ew, "_big_mover_from_dashboard",
        lambda max_price=None, min_pct=50.0: [],
    )
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "research_candidate_rows", lambda: [
        {"symbol": "SAME", "source": "xai", "score": 9.0, "reason": "thesis"},
    ])

    rows = ew._soft_seed_source_rows(_base_cfg())
    assert len([r for r in rows if r["symbol"] == "SAME"]) == 1
    assert rows[0]["source"] == "trending"


def test_maybe_soft_seed_does_not_call_desk_candidate_rows(monkeypatch):
    def _boom(cfg=None):
        raise AssertionError("desk_candidate_rows must not run from soft-seed")

    monkeypatch.setattr(ew, "desk_candidate_rows", _boom)
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg: [
        {"symbol": "SCOUT", "source": "momentum", "pct_change": 10.0,
         "dollar_volume": 3e6, "criteria": ["soft_seed", "momentum"]},
    ])
    # Poison seed-drop state; soft-seed must leave it alone.
    ew._seed_drop_counts.clear()
    ew._seed_drop_counts["movers"] = {"thin_rvol": 3}
    before = dict(ew._seed_drop_counts)

    picked, fired = ew.maybe_soft_seed_rows(
        _base_cfg(ai_watch_soft_seed_max=5), now=1_000_000.0, seen=set())
    assert fired is True
    assert [r["symbol"] for r in picked] == ["SCOUT"]
    assert picked[0].get("soft_seed") is True
    assert ew._seed_drop_counts == before


def test_shared_soft_seed_max_caps_total(monkeypatch):
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    monkeypatch.setattr(ew, "_soft_seed_source_rows", lambda cfg: [
        {"symbol": f"S{i}", "source": "trending", "pct_change": 20.0 - i,
         "dollar_volume": 5e6, "criteria": ["soft_seed", "trending"]}
        for i in range(20)
    ])
    picked, fired = ew.maybe_soft_seed_rows(
        _base_cfg(ai_watch_soft_seed_max=4), now=2_000_000.0, seen=set())
    assert fired is True
    assert len(picked) == 4


def test_maybe_soft_seed_includes_momentum_and_research_when_interval_fires(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(ew, "trading_hours_active", lambda *a, **k: True)
    (tmp_path / "trending_stocks.json").write_text(
        json.dumps({"rows": []}), encoding="utf-8")
    (tmp_path / "movers_stocks.json").write_text(
        json.dumps({"ts": 1e12, "rows": []}), encoding="utf-8")
    (tmp_path / "claude_suggestions.json").write_text(json.dumps({
        "source": "agy",
        "rows": [{"symbol": "AGY1", "score": 8.0, "reason": "board"}],
    }), encoding="utf-8")
    monkeypatch.setattr(
        ew, "_momentum_flagged_from_dashboard",
        lambda max_price=None: [(10.0, {
            "symbol": "MOM2", "score": 10.0, "source": "momentum",
            "price": 8.0, "pct_change": 18.0, "dollar_volume": 4e6,
            "criteria": ["flag"],
        })],
    )
    monkeypatch.setattr(
        ew, "_big_mover_from_dashboard",
        lambda max_price=None, min_pct=50.0: [],
    )
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (
        {"AGY1": {"price": 7.0, "pct_change": 3.0, "rvol": 2.1,
                  "day_vol": 800_000}},
        {},
    ))

    picked, fired = ew.maybe_soft_seed_rows(
        _base_cfg(ai_watch_soft_seed_max=12), now=3_000_000.0, seen=set())
    assert fired is True
    by = {r["symbol"]: r for r in picked}
    assert "MOM2" in by
    assert by["MOM2"]["source"] == "momentum"
    assert "AGY1" in by
    assert by["AGY1"]["source"] == "agy"
    assert "research" in by["AGY1"]["criteria"]
    assert by["AGY1"].get("soft_seed") is True
