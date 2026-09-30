"""No buys into a quiet tape: last-5-minute volume vs today's own pace."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import ai_entry_watch as ew

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 9, 30, 9, 30, tzinfo=ET).timestamp()


@pytest.fixture(autouse=True)
def _clean():
    ew._VOL_NOW_CACHE.clear()
    yield
    ew._VOL_NOW_CACHE.clear()


def _bars(per_min, quiet_last=0, minutes=60):
    """1m bars from 09:30; the last `quiet_last` minutes have no IEX bar."""
    return [(OPEN + i * 60, float(per_min)) for i in range(minutes - quiet_last)]


def test_steady_tape_reads_one():
    now = OPEN + 60 * 60 + 20
    got = ew.vol_now_iex("AAA", now=now, fetch=lambda s: _bars(1000))
    assert got == pytest.approx(1.0)


def test_missing_minutes_count_as_zero_volume():
    now = OPEN + 60 * 60 + 20
    got = ew.vol_now_iex("BBB", now=now, fetch=lambda s: _bars(1000, quiet_last=5))
    assert got == pytest.approx(0.0)


def test_premarket_and_forming_minute_are_ignored():
    now = OPEN + 60 * 60 + 20
    rows = _bars(1000) + [(OPEN - 600, 1e9), (OPEN + 60 * 60, 1e9)]
    got = ew.vol_now_iex("CCC", now=now, fetch=lambda s: rows)
    assert got == pytest.approx(1.0)


def test_refusal_blocks_a_quiet_tape_and_passes_a_live_one():
    cfg = {"ai_watch_min_vol_now_ratio": 0.25}
    now = OPEN + 60 * 60 + 20
    ew.vol_now_iex("QUIET", now=now, fetch=lambda s: _bars(1000, quiet_last=4))
    rec = {}
    assert ew.thin_volume_refusal(rec, "QUIET", cfg, now=now) == "vol_now_thin"
    # 1 of the last 5 minutes printed: 200/min vs 56,000 over 60 min
    assert rec["vol_now_ratio"] == pytest.approx(200 / (56000 / 60))
    ew.vol_now_iex("LIVE", now=now, fetch=lambda s: _bars(1000))
    assert ew.thin_volume_refusal({}, "LIVE", cfg, now=now) is None


def test_unknown_volume_refuses(monkeypatch):
    monkeypatch.setattr(ew, "vol_now_iex", lambda *a, **k: None)
    cfg = {"ai_watch_min_vol_now_ratio": 0.25}
    assert ew.thin_volume_refusal({}, "X", cfg, now=OPEN + 3600) == "vol_now_unknown"


def test_off_by_default_and_before_0935(monkeypatch):
    monkeypatch.setattr(ew, "vol_now_iex", lambda *a, **k: 0.0)
    assert ew.thin_volume_refusal({}, "X", {}, now=OPEN + 3600) is None
    cfg = {"ai_watch_min_vol_now_ratio": 0.25}
    assert ew.thin_volume_refusal({}, "X", cfg, now=OPEN + 120) is None
