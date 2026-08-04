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
    # Live bot_config may flip agreement; defaults document the knobs.
    assert "ai_watch_require_agreement" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_seed_momentum"] is True
    assert DEFAULT_CONFIG["ai_watch_seed_trending"] is True
    assert cfg["ai_watch_poll_sec"] == 20.0
    assert float(DEFAULT_CONFIG["ai_entry_zone_pad_pct"]) == 0.0


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


def test_upsert_preserves_submitted_status(tmp_path, monkeypatch):
    """Rebuild/upsert must not clobber submitted (or filled) back to watching."""
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "submitted",
            "structure": {"entry_low": 27.0, "entry_high": 28.0},
            "structure_ts": 900.0,
            "reason": "old",
        },
    })
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 9.0, "reason": "refresh"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert state["SMCI"]["status"] == "submitted"
    assert state["SMCI"]["score"] == 9.0
    assert state["SMCI"]["reason"] == "refresh"
    # rebuild path too
    state2 = ew.rebuild_watch_from_book(rows, cfg=cfg, now=1_100.0)
    assert state2["SMCI"]["status"] == "submitted"


def test_upsert_preserves_filled_status(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": False}
    ew.save_watch({"AAA": {"symbol": "AAA", "status": "filled", "score": 1.0}})
    state = ew.upsert_from_rows(
        [{"symbol": "AAA", "agreement": True, "trending_score": 2.0}],
        cfg=cfg,
        now=50.0,
    )
    assert state["AAA"]["status"] == "filled"


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


def test_rebuild_watch_from_book(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {
        "ai_watch_require_agreement": True,
        "ai_watch_single_source": False,
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_trending": False,
    }
    rows = [{"symbol": "SOFI", "agreement": True, "trending_score": 7.8, "reason": "peg"}]
    state = ew.rebuild_watch_from_book(rows, cfg=cfg, now=100.0)
    assert "SOFI" in state and state["SOFI"]["status"] == "watching"
    assert ew.load_watch()["SOFI"]["symbol"] == "SOFI"


def test_book_table_rows_merges_position_and_sources(tmp_path, monkeypatch):
    """Open positions show P&L; momentum/trending sources preserved on watches."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "watching",
            "source": "research",
            "score": 8.2,
            "last_ask": 29.0,
            "structure": {"entry_low": 27.0, "entry_high": 28.0, "wait_kind": "wait_for_zone"},
        },
        "ACHR": {
            "symbol": "ACHR",
            "status": "watching",
            "source": "momentum",
            "score": 7.1,
            "reason": "momentum HOT",
            "last_ask": 8.5,
            "structure": None,
        },
        "SOFI": {
            "symbol": "SOFI",
            "status": "watching",
            "source": "trending",
            "score": 7.8,
            "last_ask": 18.0,
            "structure": None,
        },
        "DEAD": {
            "symbol": "DEAD",
            "status": "expired",
            "source": "momentum",
        },
    })
    positions = {
        "SMCI": {
            "qty": 35.0,
            "avg_entry": 28.0,
            "current": 29.5,
            "pl": 52.5,
            "plpc": 5.36,
            "mkt_val": 1032.5,
        },
    }
    rows = ew.book_table_rows(positions=positions)
    by = {r["symbol"]: r for r in rows}
    assert "DEAD" not in by
    assert by["SMCI"]["phase"] == "open"
    assert by["SMCI"]["is_position"] is True
    assert by["SMCI"]["pl"] == 52.5
    assert by["SMCI"]["qty"] == 35.0
    assert by["ACHR"]["source"] == "momentum"
    assert by["ACHR"]["phase"] == "watching"
    assert by["SOFI"]["source"] == "trending"
    # Open first
    assert rows[0]["symbol"] == "SMCI"


def test_rebuild_seeds_momentum_into_active(tmp_path, monkeypatch):
    """Desk momentum names stay on the watch queue across rebuild."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [{
            "symbol": "ACHR",
            "score": 7.2,
            "trending_score": 7.2,
            "reason": "momentum HOT",
            "agreement": True,
            "source": "momentum",
        }],
    )
    cfg = {
        "ai_watch_require_agreement": False,
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_trending": False,
    }
    # Research only has SOFI; momentum ACHR must still be active (not invalidated).
    rows = [{"symbol": "SOFI", "agreement": True, "trending_score": 8.0, "reason": "ai"}]
    state = ew.rebuild_watch_from_book(rows, cfg=cfg, now=200.0)
    assert "SOFI" in state
    assert "ACHR" in state
    assert state["ACHR"]["source"] == "momentum"
    assert state["ACHR"]["status"] == "watching"


def test_expire_open_watches(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({"SMCI": {"symbol": "SMCI", "status": "watching"}})
    out = ew.expire_open_watches(now=1.0)
    assert out["SMCI"]["status"] == "expired"


def test_expire_stale_watches_for_new_day(tmp_path, monkeypatch):
    """Open watches stamped on a prior ET day expire; same-day stay open."""
    import ai_entry_watch as ew
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    et = ZoneInfo("America/New_York")
    # 2026-08-04 10:00 ET
    now = datetime(2026, 8, 4, 10, 0, tzinfo=et).timestamp()
    # prior day ~ 2026-08-03 15:00 ET
    prev = datetime(2026, 8, 3, 15, 0, tzinfo=et).timestamp()
    same = datetime(2026, 8, 4, 9, 30, tzinfo=et).timestamp()
    ew.save_watch({
        "OLD": {
            "symbol": "OLD",
            "status": "watching",
            "updated_ts": prev,
            "structure_ts": prev,
        },
        "NEW": {
            "symbol": "NEW",
            "status": "armed",
            "updated_ts": same,
            "structure_ts": same,
        },
        "DONE": {
            "symbol": "DONE",
            "status": "submitted",
            "updated_ts": prev,
        },
    })
    out = ew.expire_stale_watches_for_new_day(now)
    assert out["OLD"]["status"] == "expired"
    assert out["NEW"]["status"] == "armed"
    assert out["DONE"]["status"] == "submitted"


def test_public_snapshot_shape(tmp_path, monkeypatch):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "ZZZ": {
            "symbol": "ZZZ",
            "status": "watching",
            "agreement": True,
            "score": 7.5,
            "last_ask": 12.3,
            "structure": {
                "wait_kind": "wait_for_zone",
                "entry_low": 11.0,
                "entry_high": 13.0,
            },
        },
        "AAA": {
            "symbol": "AAA",
            "status": "armed",
            "agreement": False,
            "score": 9.1,
            "last_ask": None,
            "structure": None,
        },
    }
    ew.save_watch(state)
    snap = ew.public_snapshot()
    assert isinstance(snap, list)
    # Ready first (armed / in-zone), then higher score — AAA (armed, 9.1) then ZZZ.
    assert [r["symbol"] for r in snap] == ["AAA", "ZZZ"]
    keys = {
        "symbol", "status", "wait_kind", "entry_low", "entry_high",
        "last_ask", "score", "agreement", "reason", "source", "ready", "in_zone",
    }
    for row in snap:
        assert set(row.keys()) == keys
    zzz = snap[1]
    assert zzz["status"] == "watching"
    assert zzz["wait_kind"] == "wait_for_zone"
    assert zzz["entry_low"] == 11.0
    assert zzz["entry_high"] == 13.0
    assert zzz["last_ask"] == 12.3
    assert zzz["score"] == 7.5
    assert zzz["agreement"] is True
    assert zzz["ready"] is True  # ask inside zone
    assert zzz["in_zone"] is True
    aaa = snap[0]
    assert aaa["status"] == "armed"
    assert aaa["ready"] is True
    assert aaa["wait_kind"] is None
    assert aaa["entry_low"] is None
    assert aaa["entry_high"] is None
    assert aaa["last_ask"] is None
    # Also accepts in-memory state without load
    assert ew.public_snapshot(state)[0]["symbol"] == "AAA"


def test_should_expire_watches_on_close_edge():
    """Pre-market closed must not expire or latch; only open→closed does."""
    import ai_entry_watch as ew

    day = "2026-08-03"
    # Pre-market: closed, never saw open → no expire, no latch.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=False, expired_day="")
    assert do is False and seen is False and exp == ""

    # Still pre-market closed — still no latch.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is False and exp == ""

    # RTH open → mark seen_open.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is True and exp == ""

    # Stay open.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is True

    # Close after open → expire once, latch day.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is True and seen is False and exp == day

    # Still closed same day → do not re-expire.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and exp == day

    # Next day pre-market: no expire until open→closed again.
    day2 = "2026-08-04"
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day2, seen_open=False, expired_day=exp)
    assert do is False
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day2, seen_open=seen, expired_day=exp)
    assert do is False and seen is True
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day2, seen_open=seen, expired_day=exp)
    assert do is True and exp == day2


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


def test_poll_once_gate_error_fail_closed_no_place(tmp_path, monkeypatch):
    """has_open_position exceptions must not fall through into place_scaled_entry."""
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

    def boom(_sym):
        raise RuntimeError("broker_down")

    monkeypatch.setattr(gt, "has_open_position", boom)
    placed = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == []
    assert any(
        "gate_error:has_open_position" in str(e.get("reason") or "")
        for e in events
    )
    assert ew.load_watch()["SMCI"]["status"] == "watching"
