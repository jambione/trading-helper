"""Movers rvol pace must use the minutes the delayed SIP bar covers."""
from datetime import datetime
from zoneinfo import ZoneInfo

import movers_screener as ms
import tools.morning_funnel as mf

ET = ZoneInfo("America/New_York")


def test_minutes_follow_the_sip_delay():
    t = datetime(2026, 9, 24, 9, 52, tzinfo=ET)
    assert ms.sip_data_mins_open(t, {}) == 7.0
    assert ms.sip_data_mins_open(t, {"ai_movers_sip_delay_min": 0}) == 22.0
    assert ms.sip_data_mins_open(datetime(2026, 9, 24, 9, 35, tzinfo=ET), {}) == 1.0
    assert ms.sip_data_mins_open(datetime(2026, 9, 24, 9, 0, tzinfo=ET), {}) < 0   # premarket unchanged


def test_pfe_at_0952_clears_the_floor():
    # 2026-09-24 09:52: PFE rvol_raw 0.115 was paced over 22 min -> 0.69 (dropped).
    raw_vol, avg = 0.115, 1.0
    buggy = mf.rvol_pair(raw_vol, avg, 22.0, time_adjusted=True)[0]
    fixed = mf.rvol_pair(raw_vol, avg, ms.sip_data_mins_open(
        datetime(2026, 9, 24, 9, 52, tzinfo=ET), {}), time_adjusted=True)[0]
    assert buggy < 1.0 <= fixed
