# tests/test_ai_entry_watch.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DEFAULT_CONFIG, load_config

def test_watch_config_defaults_present():
    for key in (
        "ai_watch_enabled",
        "ai_watch_require_agreement",
        "ai_watch_single_source",
        "ai_watch_poll_sec",
        "ai_structure_ttl_sec",
        "ai_watch_expire_at_close",
        "ai_entry_zone_pad_pct",
        "ai_max_structure_calls_per_hour",
        "ai_persist_entry_decisions",
    ):
        assert key in DEFAULT_CONFIG
    cfg = load_config()
    assert cfg["ai_watch_enabled"] is True
    assert cfg["ai_watch_require_agreement"] is True
    assert cfg["ai_watch_poll_sec"] == 20.0


def test_upsert_requires_agreement_by_default(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 8.2, "reason": "ai"},
        {"symbol": "HOOD", "agreement": False, "trending_score": 7.5, "reason": "x"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert "SMCI" in state
    assert "HOOD" not in state


def test_drop_missing_invalidates(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "SMCI": {"symbol": "SMCI", "status": "watching", "updated_ts": 1.0},
        "OLD": {"symbol": "OLD", "status": "watching", "updated_ts": 1.0},
    }
    out = ew.drop_missing(state, {"SMCI"}, now=2.0)
    assert out["SMCI"]["status"] == "watching"
    assert out["OLD"]["status"] == "invalidated"


def test_ask_in_zone_with_pad():
    import ai_entry_watch as ew
    assert ew.ask_in_zone(28.0, 27.0, 28.5, 0.15) is True
    assert ew.ask_in_zone(30.0, 27.0, 28.5, 0.15) is False


def test_spread_ok_mid_pct():
    import ai_entry_watch as ew
    # (28.0 - 27.95) / mid * 100 ≈ 0.18%
    assert ew.spread_ok(27.95, 28.0, 1.0) is True
    assert ew.spread_ok(27.0, 28.0, 0.5) is False
    assert ew.spread_ok(None, 28.0, 1.0) is False
    assert ew.spread_ok(None, 28.0, 0.0) is True  # enforcement off


def test_should_arm_wait_for_zone(monkeypatch):
    import ai_entry_watch as ew
    rec = {
        "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 3.0}
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.95, cfg=cfg)
    assert ok and why == "zone"
    ok2, why2 = ew.should_arm_buy(rec, ask=32.0, bid=31.9, cfg=cfg)
    assert not ok2
    assert why2 == "above_zone"


def test_should_arm_rejects_wait_setup_and_hard_no():
    import ai_entry_watch as ew
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 3.0}
    base = {
        "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
    }
    rec_setup = {
        "status": "watching",
        "structure": {"decision": "WAIT", "wait_kind": "wait_setup", **base},
    }
    rec_hard = {
        "status": "watching",
        "structure": {"decision": "WAIT", "wait_kind": "hard_no", **base},
    }
    ok, why = ew.should_arm_buy(rec_setup, ask=28.0, bid=27.95, cfg=cfg)
    assert not ok and why == "wait_setup"
    ok2, why2 = ew.should_arm_buy(rec_hard, ask=28.0, bid=27.95, cfg=cfg)
    assert not ok2 and why2 == "hard_no"


def test_should_arm_buy_decision_in_zone():
    import ai_entry_watch as ew
    rec = {
        "status": "armed",
        "structure": {
            "decision": "BUY",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 3.0}
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.95, cfg=cfg)
    assert ok and why == "zone"


def test_should_arm_rejects_wide_spread():
    import ai_entry_watch as ew
    rec = {
        "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 0.1, "ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 3.0}
    # ~1.8% spread
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.5, cfg=cfg)
    assert not ok and why == "spread"


def _poll_cfg(**overrides):
    cfg = {
        "ai_watch_enabled": True,
        "ai_max_spread_pct": 1.0,
        "ai_entry_zone_pad_pct": 0.15,
        "ai_min_reward_risk": 3.0,
        "ai_structure_ttl_sec": 999999,
        "ai_max_structure_calls_per_hour": 12,
        "ai_max_price": 100.0,
        "ai_risk_pct": 1.0,
    }
    cfg.update(overrides)
    return cfg


def _patch_trading_ready(monkeypatch, gt, *, ask=28.0, bid=27.95):
    monkeypatch.setattr(gt, "market_is_open", lambda: True)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "_latest_ask", lambda s: ask)
    monkeypatch.setattr(gt, "_latest_bid", lambda s: bid)
    monkeypatch.setattr(gt, "has_open_position", lambda s: False)
    monkeypatch.setattr(gt, "can_open_new_position", lambda s: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"ok": True, "equity": 100_000})
    monkeypatch.setattr(gt, "buys_left_this_poll", lambda: 3)
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)


def test_poll_once_buys_when_in_zone(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    _patch_trading_ready(monkeypatch, gt)
    placed = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == ["SMCI"]
    assert any(e.get("kind") == "entry_ok" or e.get("symbol") == "SMCI" for e in events) or placed
    saved = ew.load_watch()
    assert saved["SMCI"]["status"] in ("submitted", "filled")


def test_poll_once_wide_spread_does_not_place(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    # ~1.8% spread with max 0.1%
    _patch_trading_ready(monkeypatch, gt, ask=28.0, bid=27.5)
    placed = []
    monkeypatch.setattr(
        cp, "place_scaled_entry",
        lambda *a, **k: placed.append(a[0]) or {"ok": True},
    )
    events = ew.poll_once(
        cfg=_poll_cfg(ai_max_spread_pct=0.1),
        now=1e12 + 10,
    )
    assert placed == []
    assert any(e.get("reason") == "spread" for e in events)
    assert ew.load_watch()["SMCI"]["status"] == "watching"


def test_poll_once_wait_setup_does_not_place(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_setup",
                "entry_low": 0, "entry_high": 0,
                "stop_price": 0, "target_1": 0, "reward_risk": 0,
            },
        }
    }
    ew.save_watch(state)
    _patch_trading_ready(monkeypatch, gt)
    placed = []
    evals = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True}

    def fake_eval(*a, **k):
        evals.append(1)
        return {
            "decision": "WAIT", "wait_kind": "wait_setup",
            "entry_low": 0, "entry_high": 0,
            "stop_price": 0, "target_1": 0, "reward_risk": 0,
        }

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    monkeypatch.setattr(cp, "evaluate_entry", fake_eval)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == []
    # Fresh structure within TTL → no blind restructure / no buy
    assert evals == []
    assert any(e.get("reason") == "wait_setup" for e in events)
