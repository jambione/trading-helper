"""ai_entry_earliest_time: no new positions before the operator's start (2026-10-02: 09:45)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import ai_entry_watch as ew

ET = ZoneInfo("America/New_York")


def at(hh: int, mm: int, ss: int = 0) -> float:
    return datetime(2026, 10, 5, hh, mm, ss, tzinfo=ET).timestamp()


def test_window_opens_at_the_configured_minute():
    cfg = {"ai_entry_earliest_time": "09:45"}
    assert not ew.entry_window_open(cfg, at(9, 30))
    assert not ew.entry_window_open(cfg, at(9, 44, 59))
    assert ew.entry_window_open(cfg, at(9, 45))
    assert ew.entry_window_open(cfg, at(14, 0))


def test_default_is_the_open_and_a_typo_fails_closed_to_0945():
    assert ew.entry_window_open({}, at(9, 30))
    assert not ew.entry_window_open({"ai_entry_earliest_time": "9.45"}, at(9, 40))
    assert ew.entry_window_open({"ai_entry_earliest_time": "9.45"}, at(9, 45))


def test_open_bell_waits_for_the_entry_window(monkeypatch, tmp_path):
    import ai_positions
    import ai_trader
    monkeypatch.setattr(ai_positions, "OPEN_BELL_STATE_PATH", tmp_path / "bell.json")
    cfg = {"ai_open_bell_enabled": True, "ai_open_bell_time": "09:35", "ai_entry_earliest_time": "09:45"}
    assert not ai_trader._open_bell_due(cfg, at(9, 36))
    assert ai_trader._open_bell_due(cfg, at(9, 45))
    assert ai_trader._open_bell_due({**cfg, "ai_entry_earliest_time": "09:30"}, at(9, 36))
