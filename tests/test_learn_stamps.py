"""Regime stamps for the learning loop."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import learn_stamps as ls  # noqa: E402


@pytest.fixture(autouse=True)
def _clear():
    ls.clear_caches()
    yield
    ls.clear_caches()


def test_fingerprint_stable_for_same_knobs():
    cfg = {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_exit_left_overbought": False,
        "paper": True,
        "ai_risk_pct": 1.0,
    }
    a = ls.config_fingerprint(cfg)
    b = ls.config_fingerprint(cfg)
    assert a == b
    assert len(a) == 12


def test_fingerprint_changes_with_edge_mode():
    a = ls.config_fingerprint({"ai_edge_mode": "continuation"})
    b = ls.config_fingerprint({"ai_edge_mode": "exhaustion_scalp"})
    assert a != b


def test_regime_stamp_from_cfg():
    s = ls.regime_stamp({
        "ai_edge_mode": "exhaustion_scalp",
        "ai_exit_left_overbought": False,
        "paper": True,
        "ai_trading_source": "grok",
    })
    assert s["edge_mode"] == "exhaustion_scalp"
    assert s.get("desk_product") in (None, "")
    assert s["exit_left_overbought"] is False
    assert s["paper"] is True
    assert s["book_owner"] == "grok"
    assert s["config_fp"]
    assert s["git_version"]


def test_merge_regime_fills_missing_only():
    row = {"symbol": "AAPL", "edge_mode": "keep_me"}
    out = ls.merge_regime(row, {
        "ai_edge_mode": "exhaustion_scalp",
        "paper": True,
    })
    assert out["edge_mode"] == "keep_me"
    assert out["symbol"] == "AAPL"
    assert out["paper"] is True
    assert out["config_fp"]


def test_record_outcome_includes_stamps(tmp_path, monkeypatch):
    import ai_positions as ap

    monkeypatch.setattr(ap, "OUTCOMES_PATH", tmp_path / "outcomes.jsonl")
    pos = {
        "entry_price": 10.0,
        "stop_price": 9.0,
        "total_qty": 10,
        "entry_time": 1_000.0,
        "reward_risk": 2.0,
        "entry_path": "watch",
        "edge_mode": "exhaustion_scalp",
        "exit_left_overbought": False,
        "git_version": "deadbeef",
        "config_fp": "abc123abc123",
        "paper": True,
        "book_owner": "grok",
    }
    o = ap._record_outcome("TEST", pos, 11.0, "target_hit", 1_100.0)
    assert o["entry_path"] == "watch"
    assert o["edge_mode"] == "exhaustion_scalp"
    assert o["git_version"] == "deadbeef"
    assert o["config_fp"] == "abc123abc123"
    assert o["realized_r_multiple"] == pytest.approx(1.0)
    line = (tmp_path / "outcomes.jsonl").read_text().strip()
    saved = json.loads(line)
    assert saved["edge_mode"] == "exhaustion_scalp"


def test_shadow_sample_gets_stamp(tmp_path, monkeypatch):
    import ai_positions as ap

    monkeypatch.setattr(ap, "SHADOW_PATH", tmp_path / "shadow.jsonl")
    ap.log_shadow_sample({"symbol": "ZZZ", "price": 1.0, "ts": 1.0})
    row = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
    assert row["symbol"] == "ZZZ"
    assert row.get("edge_mode") is not None or row.get("config_fp")


def test_fingerprint_covers_the_knobs_that_decide_what_a_trade_banks():
    """2026-08-19: eight of twelve knobs changed that day were not in the
    fingerprint, so the ledger would have called it the same regime."""
    import learn_stamps as ls
    keys = set(ls._FINGERPRINT_KEYS)
    for k in ("ai_watch_tv_exh_rsi", "ai_watch_min_price",
              "ai_local_trail_arm_r", "ai_local_trail_arm_pct",
              "ai_local_trail_be_at_r", "ai_local_trail_be_at_pct",
              "ai_local_trail_give_r", "ai_local_trail_give_open_r",
              "ai_local_trail_give_max_pct"):
        assert k in keys, k


def test_changing_a_trail_knob_changes_the_fingerprint():
    import learn_stamps as ls
    base = {"ai_local_trail_arm_pct": 0.15, "paper": True}
    moved = dict(base, ai_local_trail_arm_pct=0.40)
    assert ls.config_fingerprint(base) != ls.config_fingerprint(moved)


def test_changing_the_entry_rule_changes_the_fingerprint():
    import learn_stamps as ls
    a = {"ai_watch_tv_exh_rsi": True, "paper": True}
    b = {"ai_watch_tv_exh_rsi": False, "paper": True}
    assert ls.config_fingerprint(a) != ls.config_fingerprint(b)
