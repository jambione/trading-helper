"""Watch-book stream on admit: no false rest when WS tape exists.

2026-09-04: AEHG/AOUT painted rest + need-stream while Finnhub/engine held
a dated trade (stale_tape). decision_price preferred REST whenever tape age
exceeded the ceiling, disguising a quiet WS as unsubscribed.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402


def test_stream_subscribe_grace_knobs_default():
    assert DEFAULT_CONFIG["ai_watch_stream_subscribe_grace_sec"] == 90.0
    assert DEFAULT_CONFIG["ai_watch_stale_timeout_quiet_max_sec"] == 900.0


def test_decision_price_prefers_stale_tape_over_rest_when_ws_exists(monkeypatch):
    """Dated WS/engine tape must not paint as rest + need-stream."""
    monkeypatch.setattr(
        ew, "live_print", lambda sym: (6.93, 120.0))  # old but dated
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg: 15.0)

    class _GT:
        @staticmethod
        def _latest_ask(sym):
            return 6.95

        @staticmethod
        def cached_quote_age_sec(sym):
            return 2.0

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "ai_trading", _GT)
    monkeypatch.setattr("ai_trading._latest_ask", _GT._latest_ask, raising=False)
    monkeypatch.setattr(
        "ai_trading.cached_quote_age_sec", _GT.cached_quote_age_sec, raising=False)

    px, src, age = ew.decision_price("AEHG", {"ai_watch_decision_max_age_sec": 15.0})
    assert src == "stale_tape"
    assert px == 6.93
    assert age == 120.0


def test_decision_price_young_stream_still_wins(monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda sym: (40.8, 3.0))
    monkeypatch.setattr(ew, "decision_max_age_sec", lambda cfg: 15.0)
    px, src, age = ew.decision_price("SMCI", {})
    assert src == "stream" and px == 40.8 and age == 3.0


def test_quiet_dated_tape_is_not_dead_for_stale_timeout(monkeypatch):
    """A 3–10 min-old Finnhub print is quiet, not a stale_timeout drop."""
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 400.0)
    rec = {"symbol": "AEHG", "last_ask_src": "stale_tape", "admit_ts": 1.0}
    cfg = {
        "ai_watch_stale_timeout_quiet_max_sec": 900.0,
        "ai_watch_stale_timeout_include_need_stream": False,
    }
    assert not ew._stale_feed_condition(rec, "stale_tape", cfg, now=1000.0)
    # Truly ancient tape still counts as dead.
    monkeypatch.setattr(ew, "row_quote_age_sec", lambda *_a, **_k: 2000.0)
    assert ew._stale_feed_condition(rec, "stale_tape", cfg, now=1000.0)
    # quiet_max 0 disables the quiet protection.
    assert ew._stale_feed_condition(
        rec, "stale_tape",
        {**cfg, "ai_watch_stale_timeout_quiet_max_sec": 0.0},
        now=1000.0)


def test_within_subscribe_grace():
    now = 1_000_000.0
    rec = {"admit_ts": now - 30.0}
    assert ew._within_subscribe_grace(
        rec, {"ai_watch_stream_subscribe_grace_sec": 90.0}, now)
    rec["admit_ts"] = now - 120.0
    assert not ew._within_subscribe_grace(
        rec, {"ai_watch_stream_subscribe_grace_sec": 90.0}, now)


def test_ensure_watch_stream_calls_subscribe(monkeypatch):
    calls = {"pri": None, "sub": None, "push": None}

    monkeypatch.setattr(
        "finnhub_stream.set_subscribe_priority",
        lambda syms: calls.__setitem__("pri", list(syms)),
        raising=False)
    monkeypatch.setattr(
        "finnhub_stream.get_subscribe_priority",
        lambda: {"SMCI"},
        raising=False)
    monkeypatch.setattr(
        "finnhub_stream.request_subscribe",
        lambda syms: calls.__setitem__("sub", list(syms)),
        raising=False)
    monkeypatch.setattr(
        ew, "push_candidates_to_engine",
        lambda syms: calls.__setitem__("push", list(syms)) or {"pushed": len(syms)})

    out = ew.ensure_watch_stream(["AEHG", "AOUT", "bad!", "AEHG"])
    assert out["requested"] == 2
    assert "AEHG" in (calls["sub"] or [])
    assert "AOUT" in (calls["pri"] or [])
    assert "SMCI" in (calls["pri"] or [])  # merged, not wiped
