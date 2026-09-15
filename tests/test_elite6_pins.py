"""Elite-6 pinned seats + scout seats.

Pins stay on the book through soft-seed / steal thrash; scouts remain
hygiene-droppable. Promote/demote is metric-driven. Arm gates and A2 are
unchanged — pins do not bypass EXH/RSI or disable strikes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402

NOW = 1_800_000_000.0


def _reset_pin_state():
    ew._PIN_READY_ACCUM.clear()
    ew._PIN_READY_MARK.clear()
    ew._PIN_READY_DAY = ""
    ew._NO_STREAM_STRIKES.clear()
    ew._STALE_TIMEOUT_UNTIL.clear()


@pytest.fixture(autouse=True)
def _clean_pin_globals():
    _reset_pin_state()
    yield
    _reset_pin_state()


def _cp():
    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    return _CP


def _gt(held=None):
    held = set(held or [])

    class _GT:
        @staticmethod
        def has_open_position(sym):
            return sym in held

    return _GT


def _steal_cfg(**over):
    c = {
        "ai_watch_decision_max_age_sec": 15.0,
        "ai_watch_stream_subscribe_grace_sec": 90.0,
        "ai_watch_stale_timeout_grace_sec": 90.0,
        "ai_watch_warming_seats": 3,
        "ai_watch_pin_slots": 6,
        "ai_watch_pin_protect_steals": True,
        "ai_watch_pin_min_dollar_volume": 2e6,
        "ai_watch_pin_min_stream_ready_sec": 120.0,
        "ai_watch_pin_demote_dead_sec": 600.0,
    }
    c.update(over)
    return c


def _stale_pin(sym="PIN1", *, dvol=50e3, admit_age=600.0):
    return {
        "symbol": sym,
        "status": "watching",
        "seat_role": "pin",
        "source": "momentum",
        "last_ask_src": "stale_tape",
        "admit_dollar_volume": dvol,
        "last_ask_age_sec": 400.0,
        "admit_ts": NOW - admit_age,
        "stale_tape_streak": 4,
        "block_code": "stale_quote",
        "pin_stream_ready_sec": 300.0,
    }


def _fresh_cand(sym="FRESH"):
    return {
        "symbol": sym,
        "last_ask_src": "stream",
        "last_ask_age_sec": 4.0,
        "dollar_volume": 9e6,
        "seat_role": "warming",
        "soft_seed": True,
    }


# ── config ──────────────────────────────────────────────────────────────────


def test_pin_config_defaults():
    assert DEFAULT_CONFIG["ai_watch_pin_slots"] == 6
    assert DEFAULT_CONFIG["ai_watch_scout_seats"] == 3
    assert DEFAULT_CONFIG["ai_watch_pin_min_dollar_volume"] == 2e6
    assert DEFAULT_CONFIG["ai_watch_pin_min_stream_ready_sec"] == 120.0
    assert DEFAULT_CONFIG["ai_watch_pin_demote_dead_sec"] == 600.0
    assert DEFAULT_CONFIG["ai_watch_pin_protect_steals"] is True
    assert DEFAULT_CONFIG["ai_watch_max_entries_per_symbol_day"] == 0
    assert ew.pin_slot_quota({}) == 6
    assert ew.scout_seat_quota({}) == 3
    assert ew.scout_seat_quota({"ai_watch_warming_seats": 4}) == 4
    assert ew.scout_seat_quota(
        {"ai_watch_warming_seats": 4, "ai_watch_scout_seats": 2}) == 2


def test_pin_knobs_in_dashboard_allowlist():
    import config
    src = Path(config.__file__).read_text(encoding="utf-8")
    for k in (
        "ai_watch_pin_slots",
        "ai_watch_scout_seats",
        "ai_watch_pin_min_dollar_volume",
        "ai_watch_pin_min_stream_ready_sec",
        "ai_watch_pin_demote_dead_sec",
        "ai_watch_pin_protect_steals",
    ):
        assert f'"{k}"' in src


# ── steal / cap immunity ────────────────────────────────────────────────────


def test_preheat_steal_skips_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "row_quote_age_sec", lambda rec, now=None: rec.get("last_ask_age_sec"))
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    state = {
        "PIN1": _stale_pin("PIN1"),
        "SCOUT_DEAD": {
            "symbol": "SCOUT_DEAD", "status": "watching",
            "seat_role": "warming",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 10e3,
            "last_ask_age_sec": 500.0, "admit_ts": NOW - 600.0,
            "stale_tape_streak": 5, "block_code": "stale_quote",
        },
    }
    ew.save_watch(state)
    events: list = []
    dropped = ew._preferential_preheat_steal(
        state, cfg=_steal_cfg(), now=NOW, events=events, cp=_cp(), gt=_gt(),
        candidates=[_fresh_cand()])
    assert "PIN1" not in dropped
    assert "SCOUT_DEAD" in dropped
    assert "PIN1" in ew.load_watch()


def test_unarmable_steal_skips_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "row_quote_age_sec", lambda rec, now=None: rec.get("last_ask_age_sec"))
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    state = {
        "PIN1": _stale_pin("PIN1"),
        "DEAD": {
            "symbol": "DEAD", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 20e3,
            "last_ask_age_sec": 450.0, "admit_ts": NOW - 600.0,
            "stale_tape_streak": 3, "block_code": "stale_quote",
        },
    }
    ew.save_watch(state)
    dropped = ew._preferential_unarmable_steal(
        state, cfg=_steal_cfg(), now=NOW, events=[], cp=_cp(), gt=_gt(),
        candidates=[_fresh_cand()])
    assert dropped == ["DEAD"]
    assert "PIN1" in ew.load_watch()


def test_stale_tape_cap_skips_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "row_quote_age_sec", lambda rec, now=None: rec.get("last_ask_age_sec"))
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    state = {
        "PIN1": {
            **_stale_pin("PIN1", dvol=1e3),
            # Cap only looks at stale_tape src; pin must still be skipped.
        },
        "STALE_A": {
            "symbol": "STALE_A", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 5e6,
            "last_ask_age_sec": 80.0,
        },
        "STALE_B": {
            "symbol": "STALE_B", "status": "watching",
            "last_ask_src": "stale_tape", "admit_dollar_volume": 100e3,
            "last_ask_age_sec": 200.0,
        },
    }
    ew.save_watch(state)
    dropped = ew._enforce_stale_tape_seat_cap(
        state, cfg={**_steal_cfg(), "ai_watch_max_stale_tape_seats": 1},
        now=NOW, events=[], cp=_cp(), gt=_gt(),
        candidates=[_fresh_cand()])
    assert "PIN1" not in dropped
    assert "STALE_B" in dropped
    assert "PIN1" in ew.load_watch()


def test_protected_pin_helper_respects_flag():
    rec = _stale_pin()
    assert ew._is_protected_pin_seat(rec, _steal_cfg(), now=NOW) is True
    assert ew._is_protected_pin_seat(
        rec, _steal_cfg(ai_watch_pin_protect_steals=False), now=NOW) is False
    rec2 = {**rec, "seat_role": "warming"}
    assert ew._is_protected_pin_seat(rec2, _steal_cfg(), now=NOW) is False


# ── sync keep ───────────────────────────────────────────────────────────────


def _cand(sym, **over):
    r = {
        "symbol": sym, "source": "momentum", "price": 5.0,
        "pct_change": 12.0, "rvol": 6.0, "criteria": ["mom_open"],
        "dollar_volume": 5e6,
    }
    r.update(over)
    return r


def _sync(monkeypatch, tmp_path, candidates, cfg, prior=None):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "ensure_watch_stream", lambda *_a, **_k: {})
    monkeypatch.setattr(ew, "live_print", lambda *_a, **_k: None)
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "push_candidates_to_engine", lambda *_a, **_k: {})
    if prior is not None:
        ew.save_watch(prior)
    return ew._sync_watch_locked(candidates, NOW, cfg)


def test_sync_keeps_pin_off_panel(monkeypatch, tmp_path):
    """Scout off-panel drops; pin stays even with grace=0."""
    prior = {
        "PIN1": {
            "symbol": "PIN1", "status": "watching", "seat_role": "pin",
            "source": "momentum", "last_candidate_ts": NOW - 900,
            "admit_dollar_volume": 5e6, "pin_stream_ready_sec": 400.0,
            "last_ask_src": "stream", "last_ask_age_sec": 3.0,
        },
        "SCOUT1": {
            "symbol": "SCOUT1", "status": "watching", "seat_role": "warming",
            "source": "movers", "last_candidate_ts": NOW - 900,
        },
    }
    got = _sync(
        monkeypatch, tmp_path, [_cand("BBB")],
        {"ai_watch_admit_grace_sec": 0.0, "ai_watch_pin_slots": 6},
        prior)
    assert "PIN1" in got
    assert got["PIN1"].get("seat_role") == "pin"
    assert "SCOUT1" not in got


def test_sync_preserves_pin_over_warming_tag(monkeypatch, tmp_path):
    prior = {
        "PIN1": {
            "symbol": "PIN1", "status": "watching", "seat_role": "pin",
            "source": "momentum", "last_candidate_ts": NOW - 10,
            "admit_dollar_volume": 8e6, "pin_stream_ready_sec": 500.0,
            "last_ask_src": "stream", "last_ask_age_sec": 2.0,
        },
    }
    got = _sync(
        monkeypatch, tmp_path,
        [_cand("PIN1", seat_role="warming")],
        {"ai_watch_admit_grace_sec": 0.0, "ai_watch_pin_slots": 6},
        prior)
    assert got["PIN1"].get("seat_role") == "pin"


# ── promote / demote ────────────────────────────────────────────────────────


def test_promote_fills_pin_slots():
    cfg = _steal_cfg(ai_watch_pin_slots=2, ai_watch_pin_min_stream_ready_sec=100.0)
    state = {}
    for i, sym in enumerate(("AAA", "BBB", "CCC")):
        state[sym] = {
            "symbol": sym, "status": "watching", "source": "momentum",
            "seat_role": "warming",
            "last_ask_src": "stream", "last_ask_age_sec": 3.0,
            "admit_dollar_volume": 5e6 + i * 1e6,
            "pin_stream_ready_sec": 200.0 + i * 10,
        }
        ew._PIN_READY_ACCUM[sym] = 200.0 + i * 10
    events: list = []
    promoted = ew._apply_pin_roles(
        state, cfg=cfg, now=NOW, events=events, cp=_cp())
    assert len(promoted) == 2
    pins = [
        s for s, r in state.items()
        if str(r.get("seat_role") or "") == "pin"
    ]
    assert len(pins) == 2
    # Highest ready_sec first: CCC, BBB
    assert set(pins) == {"CCC", "BBB"}
    assert any(e.get("kind") == "pin_promote" for e in events)


def test_demote_dead_frees_pin_slot():
    cfg = _steal_cfg(ai_watch_pin_demote_dead_sec=600.0)
    state = {
        "DEADPIN": {
            "symbol": "DEADPIN", "status": "watching", "seat_role": "pin",
            "source": "momentum",
            "last_ask_src": "stale_tape", "last_ask_age_sec": 400.0,
            "admit_dollar_volume": 5e6,
            "pin_dead_since": NOW - 700.0,
            "pin_stream_ready_sec": 500.0,
        },
    }
    events: list = []
    ew._apply_pin_roles(state, cfg=cfg, now=NOW, events=events, cp=_cp())
    assert state["DEADPIN"].get("seat_role") == "warming"
    assert any(
        e.get("kind") == "pin_demote" and e.get("reason") == "dead_timeout"
        for e in events
    )


def test_a2_demoted_symbol_not_promoted_to_pin():
    cfg = _steal_cfg(
        ai_watch_pin_slots=2,
        ai_watch_no_stream_strike_limit=2,
    )
    day = ew._et_day_key(NOW)
    ew._NO_STREAM_STRIKES[day] = {"BAD": 3}
    state = {
        "BAD": {
            "symbol": "BAD", "status": "watching", "source": "momentum",
            "seat_role": "warming",
            "last_ask_src": "stream", "last_ask_age_sec": 2.0,
            "admit_dollar_volume": 9e6,
            "pin_stream_ready_sec": 999.0,
        },
    }
    ew._PIN_READY_ACCUM["BAD"] = 999.0
    promoted = ew._apply_pin_roles(
        state, cfg=cfg, now=NOW, events=[], cp=_cp())
    assert promoted == []
    assert state["BAD"].get("seat_role") != "pin"


def test_a2_demote_strips_existing_pin():
    cfg = _steal_cfg(ai_watch_no_stream_strike_limit=2)
    day = ew._et_day_key(NOW)
    ew._NO_STREAM_STRIKES[day] = {"PIN1": 2}
    state = {
        "PIN1": {
            "symbol": "PIN1", "status": "watching", "seat_role": "pin",
            "source": "momentum",
            "last_ask_src": "stream", "last_ask_age_sec": 2.0,
            "admit_dollar_volume": 5e6,
        },
    }
    events: list = []
    ew._apply_pin_roles(state, cfg=cfg, now=NOW, events=events, cp=_cp())
    assert state["PIN1"].get("seat_role") == "warming"
    assert any(
        e.get("kind") == "pin_demote" and e.get("reason") == "a2_demoted"
        for e in events
    )


def test_research_only_not_promoted_by_default():
    cfg = _steal_cfg(ai_watch_pin_slots=3)
    state = {
        "RES": {
            "symbol": "RES", "status": "watching", "source": "research",
            "seat_role": "warming",
            "last_ask_src": "stream", "last_ask_age_sec": 2.0,
            "admit_dollar_volume": 20e6,
            "pin_stream_ready_sec": 900.0,
        },
    }
    ew._PIN_READY_ACCUM["RES"] = 900.0
    assert ew._apply_pin_roles(
        state, cfg=cfg, now=NOW, events=[], cp=_cp()) == []


# ── arm gates unchanged ─────────────────────────────────────────────────────


def test_pin_does_not_bypass_exhaustion_gate():
    """seat_role=pin must not appear in exhaustion_allows_buy logic."""
    import inspect
    src = inspect.getsource(ew.exhaustion_allows_buy)
    assert "seat_role" not in src


def test_max_entries_per_symbol_day_still_zero():
    assert DEFAULT_CONFIG["ai_watch_max_entries_per_symbol_day"] == 0
