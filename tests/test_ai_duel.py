"""Daily A vs X duel: champions, R score, winner-only chance 3."""
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
    return path


def test_register_champions_a_and_x(duel_path):
    cfg = {"ai_duel_enabled": True}
    # Fixed "now" midday trial (before 12:45 ET) — use a Monday morning ET.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    a = duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.0, "reason": "a thesis", "source": "anthropic"}],
        source="anthropic",
        cfg=cfg,
        now=now,
    )
    x = duel.register_champion_from_rows(
        [{"symbol": "SOFI", "score": 8.5, "reason": "x thesis", "source": "xai"}],
        source="xai",
        cfg=cfg,
        now=now,
    )
    assert a["symbol"] == "CMG" and a["source_mark"] == "A"
    assert x["symbol"] == "SOFI" and x["source_mark"] == "X"
    st = duel.load_state(now)
    assert st["phase"] == "trial"
    assert set(st["champions"]) == {"anthropic", "xai"}


def test_score_higher_r_wins(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_am = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    cfg = {
        "ai_duel_enabled": True,
        "ai_duel_trial_end_time": "14:15",
        "ai_duel_chance3_time": "14:30",
    }
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=cfg, now=now_am)
    duel.register_champion_from_rows(
        [{"symbol": "BBB", "score": 9}], source="xai", cfg=cfg, now=now_am)
    duel.note_entry("AAA", source="anthropic", entry_price=10.0, stop_price=9.0, now=now_am)
    duel.note_entry("BBB", source="xai", entry_price=10.0, stop_price=9.0, now=now_am)

    # A exits +1R, X exits -0.5R
    monkeypatch.setattr(
        "alpaca_trader.get_positions_detail",
        lambda: {
            "AAA": {"qty": 10, "avg_entry": 10.0, "current": 11.0, "pl": 10.0},
            "BBB": {"qty": 10, "avg_entry": 10.0, "current": 9.5, "pl": -5.0},
        },
    )
    closed = []

    def _close(sym, **kw):
        closed.append(sym)
        return {"ok": True}

    monkeypatch.setattr("alpaca_trader.close_out", _close)
    monkeypatch.setattr(
        "ai_positions._load_state",
        lambda: {
            "AAA": {"entry_price": 10.0, "stop_price": 9.0, "total_qty": 10},
            "BBB": {"entry_price": 10.0, "stop_price": 9.0, "total_qty": 10},
        },
    )

    now_pm = datetime(2026, 8, 3, 14, 20, tzinfo=et).timestamp()
    out = duel.run_trial_liquidate_and_score(cfg, now_pm)
    assert out["ok"] is True
    assert out["winner"] == "anthropic"
    assert out["r_anthropic"] == pytest.approx(1.0)
    assert out["r_xai"] == pytest.approx(-0.5)
    assert set(closed) == {"AAA", "BBB"}
    st = duel.load_state(now_pm)
    assert st["phase"] == "scored"
    # After score, only winner may enter
    assert duel.allow_entry_for_source(cfg, "anthropic", "ZZZ", now=now_pm) is False
    # Chance 3 not open until 14:30
    assert duel.allow_entry_for_source(cfg, "anthropic", "AAA", now=now_pm) is False
    now_c3 = datetime(2026, 8, 3, 14, 35, tzinfo=et).timestamp()
    assert duel.allow_entry_for_source(cfg, "xai", "BBB", now=now_c3) is False
    # Winner can register chance-3 champion
    c3 = duel.register_champion_from_rows(
        [{"symbol": "WIN", "score": 9.2}],
        source="anthropic",
        cfg=cfg,
        now=now_c3,
    )
    assert c3 and c3["symbol"] == "WIN" and c3["chance"] == 3
    assert duel.allow_entry_for_source(cfg, "anthropic", "WIN", now=now_c3) is True


def test_tie_r_no_winner(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_am = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    cfg = {"ai_duel_enabled": True, "ai_duel_trial_end_time": "14:15"}
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=cfg, now=now_am)
    duel.register_champion_from_rows(
        [{"symbol": "BBB", "score": 9}], source="xai", cfg=cfg, now=now_am)
    monkeypatch.setattr("alpaca_trader.get_positions_detail", lambda: {})
    monkeypatch.setattr("alpaca_trader.close_out", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("ai_positions._load_state", lambda: {})
    now_pm = datetime(2026, 8, 3, 14, 20, tzinfo=et).timestamp()
    out = duel.run_trial_liquidate_and_score(cfg, now_pm)
    assert out["winner"] is None
    assert duel.load_state(now_pm)["phase"] == "done"


def test_compute_r():
    assert duel._compute_r(10, 9, 11) == pytest.approx(1.0)
    assert duel._compute_r(10, 9, 9.5) == pytest.approx(-0.5)
    assert duel._compute_r(10, 10, 11) is None


def test_reject_duplicate_champion_symbol(duel_path, monkeypatch):
    """A and X must not share the same champion ticker."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    cfg = {"ai_duel_enabled": True}
    monkeypatch.setattr(
        "ai_entry_watch.load_watch", lambda: {})
    monkeypatch.setattr(
        "ai_entry_watch.save_watch", lambda st: None)
    a = duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.0}, {"symbol": "SOFI", "score": 8.0}],
        source="anthropic", cfg=cfg, now=now,
    )
    assert a["symbol"] == "CMG"
    x = duel.register_champion_from_rows(
        [{"symbol": "CMG", "score": 9.5}, {"symbol": "SOFI", "score": 8.0}],
        source="xai", cfg=cfg, now=now,
    )
    assert x is not None
    assert x["symbol"] == "SOFI"  # skipped CMG, took next free


def test_note_close_freezes_r(duel_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    cfg = {"ai_duel_enabled": True}
    duel.register_champion_from_rows(
        [{"symbol": "AAA", "score": 9}], source="anthropic", cfg=cfg, now=now)
    duel.note_entry("AAA", source="anthropic", entry_price=10.0, stop_price=9.0, now=now)
    closed = duel.note_close(
        "AAA", exit_price=11.0, entry_price=10.0, stop_price=9.0,
        source="anthropic", now=now + 60,
    )
    assert closed["status"] == "closed"
    assert closed["realized_r"] == pytest.approx(1.0)
    # Idempotent
    again = duel.note_close(
        "AAA", exit_price=12.0, source="anthropic", now=now + 120)
    assert again["realized_r"] == pytest.approx(1.0)


def test_research_allowed_winner_only_after_c3(duel_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    cfg = {
        "ai_duel_enabled": True,
        "ai_duel_trial_end_time": "14:15",
        "ai_duel_chance3_time": "14:30",
    }
    now_am = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    assert duel.research_allowed_for_source(cfg, "anthropic", now=now_am)
    assert duel.research_allowed_for_source(cfg, "xai", now=now_am)

    # Fake scored state with A as winner
    path = duel.DUEL_STATE_PATH
    path.write_text(json.dumps({
        "day": "2026-08-03",
        "phase": "scored",
        "winner": "anthropic",
        "trial_liquidated": True,
        "champions": {},
        "score": {},
    }), encoding="utf-8")
    now_c3 = datetime(2026, 8, 3, 14, 35, tzinfo=et).timestamp()
    assert duel.research_allowed_for_source(cfg, "anthropic", now=now_c3) is True
    assert duel.research_allowed_for_source(cfg, "xai", now=now_c3) is False
