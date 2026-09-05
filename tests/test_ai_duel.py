"""Daily A vs X duel: window cuts 10m before research, R score, winner C3."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ai_duel as duel  # noqa: E402


@pytest.fixture()
def duel_path(tmp_path, monkeypatch):
    path = tmp_path / "duel_state.json"
    monkeypatch.setattr(duel, "DUEL_STATE_PATH", path)
    # Do not append duel_* noise into production events.jsonl.
    monkeypatch.setattr(
        "ai_positions.log_event",
        lambda *a, **k: {"ok": True},
        raising=False,
    )
    return path


CFG = {
    "ai_duel_enabled": True,
    "ai_duel_close_before_research_min": 10,
    "claude_research_times": ["08:30", "11:30", "14:30"],
    "grok_research_times": ["08:30", "11:30", "14:30"],
}


def test_duel_enabled_defaults_false_when_key_missing():
    """Missing key must not silently re-enable champion-only gates."""
    assert duel.duel_enabled({}) is False
    assert duel.duel_enabled(None) is False
    assert duel.duel_enabled({"ai_duel_enabled": False}) is False
    assert duel.duel_enabled({"ai_duel_enabled": True}) is True


def test_window_cuts_ten_min_before_research():
    cuts = duel.window_cuts(CFG)
    keys = [k for k, _, _ in cuts]
    assert keys == ["11:20", "14:20"]
    assert cuts[0][2] is False  # mid
    assert cuts[1][2] is True   # final before C3


def test_register_champions_a_and_x(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr("ai_entry_watch.save_watch", lambda st: None)
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    a = duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.0, "reason": "a thesis", "source": "anthropic"}],
        source="anthropic",
        cfg=CFG,
        now=now,
    )
    x = duel.register_champion_from_rows(
        [{"symbol": "SOFI", "score": 8.5, "reason": "x thesis", "source": "xai"}],
        source="xai",
        cfg=CFG,
        now=now,
    )
    assert a["symbol"] == "CMG" and a["source_mark"] == "G"
    assert x["symbol"] == "SOFI" and x["source_mark"] == "X"


def test_mid_window_then_final_winner(duel_path, monkeypatch):
    """11:20 mid cut accumulates R; 14:20 final picks C3 winner by sum R."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr("ai_entry_watch.save_watch", lambda st: None)
    et = ZoneInfo("America/New_York")
    now_am = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=CFG, now=now_am)
    duel.register_champion_from_rows(
        [{"symbol": "BBB", "score": 9}], source="xai", cfg=CFG, now=now_am)
    duel.note_entry("AAA", source="anthropic", entry_price=10.0, stop_price=9.0, now=now_am)
    duel.note_entry("BBB", source="xai", entry_price=10.0, stop_price=9.0, now=now_am)

    monkeypatch.setattr(
        "alpaca_trader.get_positions_detail",
        lambda: {
            "AAA": {"qty": 10, "avg_entry": 10.0, "current": 11.0, "pl": 10.0},
            "BBB": {"qty": 10, "avg_entry": 10.0, "current": 9.5, "pl": -5.0},
        },
    )
    monkeypatch.setattr("alpaca_trader.close_out", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "ai_positions._load_state",
        lambda: {
            "AAA": {"entry_price": 10.0, "stop_price": 9.0, "total_qty": 10},
            "BBB": {"entry_price": 10.0, "stop_price": 9.0, "total_qty": 10},
        },
    )

    # Mid cut 11:20
    now_mid = datetime(2026, 8, 3, 11, 20, tzinfo=et).timestamp()
    assert duel.trial_liquidate_due(CFG, now_mid)
    out1 = duel.run_window_liquidate_and_score(CFG, now_mid)
    assert out1["ok"] and out1["final"] is False
    assert out1["cut"] == "11:20"
    st = duel.load_state(now_mid)
    assert st["phase"] == "trial"
    assert st["trial_liquidated"] is False
    assert "11:20" in st["windows_scored"]
    assert st["totals"]["agy"] == pytest.approx(1.0)
    assert st["totals"]["xai"] == pytest.approx(-0.5)

    # After close, can register next window champions
    now_1130 = datetime(2026, 8, 3, 11, 35, tzinfo=et).timestamp()
    a2 = duel.register_champion_from_rows(
        [{"symbol": "CCC", "score": 8}], source="anthropic", cfg=CFG, now=now_1130)
    assert a2 and a2["symbol"] == "CCC"

    # Final cut 14:20 — no open positions this window → 0R, A still wins on totals
    monkeypatch.setattr("alpaca_trader.get_positions_detail", lambda: {})
    now_final = datetime(2026, 8, 3, 14, 20, tzinfo=et).timestamp()
    out2 = duel.run_window_liquidate_and_score(CFG, now_final)
    assert out2["final"] is True
    assert out2["winner"] == "agy"
    st2 = duel.load_state(now_final)
    assert st2["phase"] == "scored"
    assert st2["trial_liquidated"] is True


def test_reject_duplicate_champion_symbol(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr("ai_entry_watch.save_watch", lambda st: None)
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.0}, {"symbol": "SOFI", "score": 8.0}],
        source="anthropic", cfg=CFG, now=now,
    )
    x = duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.5}, {"symbol": "SOFI", "score": 8.0}],
        source="xai", cfg=CFG, now=now,
    )
    assert x["symbol"] == "SOFI"


def test_note_close_freezes_r(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr("ai_entry_watch.save_watch", lambda st: None)
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=CFG, now=now)
    duel.note_entry("AAA", source="anthropic", entry_price=10.0, stop_price=9.0, now=now)
    closed = duel.note_close(
        "AAA", exit_price=11.0, entry_price=10.0, stop_price=9.0,
        source="anthropic", now=now + 60,
    )
    assert closed["realized_r"] == pytest.approx(1.0)


def test_research_allowed_winner_only_after_c3(duel_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_am = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    assert duel.research_allowed_for_source(CFG, "anthropic", now=now_am)
    path = duel.DUEL_STATE_PATH
    path.write_text(json.dumps({
        "day": "2026-08-03",
        "phase": "scored",
        "winner": "agy",
        "trial_liquidated": True,
        "champions": {},
        "score": {},
    }), encoding="utf-8")
    now_c3 = datetime(2026, 8, 3, 14, 35, tzinfo=et).timestamp()
    assert duel.research_allowed_for_source(CFG, "anthropic", now=now_c3) is True
    assert duel.research_allowed_for_source(CFG, "xai", now=now_c3) is False


def test_compute_r():
    assert duel._compute_r(10, 9, 11) == pytest.approx(1.0)
    assert duel._compute_r(10, 9, 9.5) == pytest.approx(-0.5)
    assert duel._compute_r(10, 10, 11) is None


def test_register_blocked_while_leg_open(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr("ai_entry_watch.save_watch", lambda st: None)
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=CFG, now=now)
    duel.note_entry("AAA", source="anthropic", entry_price=10.0, stop_price=9.0, now=now)
    again = duel.register_champion_from_rows(
        [{"symbol": "BBB", "score": 9.5}], source="anthropic", cfg=CFG, now=now + 60)
    assert again is None
    st = duel.load_state(now)
    assert st["champions"]["agy"]["symbol"] == "AAA"


def test_save_state_writes_file(duel_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    st = duel.load_state(now)
    st["phase"] = "trial"
    st["totals"] = {"agy": 1.5, "xai": -0.25}
    duel.save_state(st)
    assert duel_path.exists()
    raw = json.loads(duel_path.read_text(encoding="utf-8"))
    assert raw["totals"]["agy"] == pytest.approx(1.5)
