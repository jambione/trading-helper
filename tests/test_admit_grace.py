"""A name must not lose its row for failing one filter on one cycle.

_sync_watch_locked rebuilds the book from THIS cycle's candidate list, so a
symbol that momentarily fails any inclusion filter is dropped entirely — with
its zone structure, its admit stamp and its arm streak. Marginal names
flicker: rvol crossing 2.0, or pct_change crossing zero, drops and re-adds the
same symbol every cycle.

That was survivable while arming needed one good poll. It is not now.
ai_watch_arm_confirm_ticks asks for CONSECUTIVE agreeing polls, and a name
that leaves the book between two of them can never accumulate any — the
confirmation would quietly exclude exactly the borderline names it was never
aimed at.

Measured 2026-08-28: 311 watch_drop events in one session, GAP 153 and BULL
153, from a different cause (the attempt cap racing the seeder) but the same
shape — a book that rebuilds rather than persists.
"""
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402

NOW = 1_787_000_000.0


def _cand(sym, **over):
    r = {"symbol": sym, "source": "momentum", "price": 5.0,
         "pct_change": 12.0, "rvol": 6.0, "criteria": ["mom_open"]}
    r.update(over)
    return r


def _sync(monkeypatch, tmp_path, candidates, cfg, prior=None):
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    if prior is not None:
        ew.save_watch(prior)
    return ew._sync_watch_locked(candidates, NOW, cfg)


def _cfg(**over):
    c = {"ai_watch_admit_grace_sec": 120.0}
    c.update(over)
    return c


def test_a_dropped_candidate_is_carried_through_the_grace(monkeypatch, tmp_path):
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 30, "arm_streak": 1}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")], _cfg(), prior)
    assert "AAA" in got, "a name gone for 30s with a 120s grace must survive"


def test_the_arm_streak_survives_with_it(monkeypatch, tmp_path):
    """The whole reason the grace exists — the streak is the fragile part."""
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 10, "arm_streak": 1}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")], _cfg(), prior)
    assert int((got.get("AAA") or {}).get("arm_streak") or 0) == 1


def test_a_name_gone_past_the_grace_is_dropped(monkeypatch, tmp_path):
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 500}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")], _cfg(), prior)
    assert "AAA" not in got, "the grace is a delay, not a permanent hold"


def test_zero_grace_rebuilds_strictly_from_candidates(monkeypatch, tmp_path):
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 1}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")],
                _cfg(ai_watch_admit_grace_sec=0.0), prior)
    assert "AAA" not in got


def test_a_current_candidate_gets_its_stamp_refreshed(monkeypatch, tmp_path):
    """Without the refresh the grace would measure from admission and expire
    on a name the panels are still offering every cycle."""
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 900}}
    got = _sync(monkeypatch, tmp_path, [_cand("AAA")], _cfg(), prior)
    assert (got.get("AAA") or {}).get("last_candidate_ts") == NOW


def test_a_record_with_no_stamp_is_not_held_forever(monkeypatch, tmp_path):
    """Absence of a timestamp must not read as 'seen just now'."""
    prior = {"AAA": {"symbol": "AAA", "status": "watching"}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")], _cfg(), prior)
    assert "AAA" not in got


def test_the_grace_does_not_bypass_dead_reentry(monkeypatch, tmp_path):
    """A name the desk has banned must not come back through the side door."""
    monkeypatch.setattr(ew, "_dead_reentry_blocked", lambda s, t, c: s == "AAA")
    prior = {"AAA": {"symbol": "AAA", "status": "watching",
                     "last_candidate_ts": NOW - 5}}
    got = _sync(monkeypatch, tmp_path, [_cand("BBB")], _cfg(), prior)
    assert "AAA" not in got


def test_it_is_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_admit_grace_sec"] == 0.0


def test_the_knob_reaches_the_live_config():
    import config
    assert "ai_watch_admit_grace_sec" in config.load_config()
