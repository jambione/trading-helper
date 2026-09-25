"""Room-below-the-day's-high arm gate (off by default)."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(hh, mm):
    return datetime(2026, 9, 25, hh, mm, tzinfo=ET).timestamp()


def _bars(rows):
    return pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close", "volume"],
                        index=pd.to_datetime([r[0] for r in rows], unit="s", utc=True))


def test_day_high_uses_only_todays_completed_session_bars(monkeypatch):
    import alpaca_api as aa
    rows = [(_t(9, 0), 10, 99.0, 9, 10, 1),       # premarket spike: ignored
            (_t(9, 30), 10, 10.5, 9.9, 10.2, 1),
            (_t(10, 0), 10, 11.0, 9.9, 10.8, 1),
            (_t(10, 1), 10, 12.0, 9.9, 11.0, 1)]  # still forming at 10:01:30: ignored
    monkeypatch.setattr(aa, "fetch_bars", lambda c, s, cfg: _bars(rows))
    monkeypatch.setattr(ew, "_data_client", lambda: None)
    monkeypatch.setattr(ew, "_DAY_HIGH_CACHE", {})
    assert ew.day_high_iex("abc", now=_t(10, 1) + 30) == 11.0


def test_gate_is_off_by_default():
    assert ew.room_below_hod_refusal({}, "ABC", 10.0, {}) is None


def test_gate_refuses_near_the_high_and_allows_room(monkeypatch):
    monkeypatch.setattr(ew, "day_high_iex", lambda s, now=None: 100.0)
    cfg = {"ai_watch_min_room_below_hod_pct": 2.6}
    rec = {}
    assert ew.room_below_hod_refusal(rec, "ABC", 99.0, cfg) == "near_hod"
    assert "from day high" in rec["block_detail"]
    assert ew.room_below_hod_refusal({}, "ABC", 97.0, cfg) is None


def test_unknown_high_refuses_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(ew, "day_high_iex", lambda s, now=None: None)
    assert ew.room_below_hod_refusal({}, "ABC", 97.0,
                                     {"ai_watch_min_room_below_hod_pct": 2.6}) == "hod_unknown"
