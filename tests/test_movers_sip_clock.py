"""Movers rvol pace: today's SIP daily bar is served in real time; pace it by the clock (2026-10-14)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import movers_screener as ms
import tools.morning_funnel as mf

ET = ZoneInfo("America/New_York")


def test_minutes_follow_the_clock_less_the_daily_bar_lag():
    t = datetime(2026, 9, 24, 9, 52, tzinfo=ET)
    assert ms.sip_data_mins_open(t, {}) == 21.0                                   # 22 min open, 1 min lag
    assert ms.sip_data_mins_open(t, {"ai_movers_daily_bar_lag_min": 0}) == 22.0
    assert ms.sip_data_mins_open(t, {"ai_movers_daily_bar_lag_min": 15}) == 7.0  # the 9/24 behaviour
    assert ms.sip_data_mins_open(t, {"ai_movers_sip_delay_min": 15}) == 21.0     # the minute-quote delay no longer paces rvol
    assert ms.sip_data_mins_open(datetime(2026, 9, 24, 9, 31, tzinfo=ET), {}) == 1.0
    assert ms.sip_data_mins_open(datetime(2026, 9, 24, 9, 0, tzinfo=ET), {}) < 0   # premarket unchanged


def test_real_time_volume_is_not_inflated_at_10():
    # 10:00 ET, a name trading exactly at its usual pace: rvol must read ~1.0.
    avg = 1_000_000.0
    mins = 30.0
    vol = avg * mf.expected_fraction(mins)                # real-time volume, normal pace
    fixed = mf.rvol_pair(vol, avg, ms.sip_data_mins_open(datetime(2026, 10, 6, 10, 0, tzinfo=ET), {}), time_adjusted=True)[0]
    old = mf.rvol_pair(vol, avg, 15.0, time_adjusted=True)[0]
    assert 0.9 <= fixed <= 1.1
    assert old > 1.5                                      # what the clock - 15 pacing reported
