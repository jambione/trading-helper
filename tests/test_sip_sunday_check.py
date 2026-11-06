"""SIP go/no-go decision logic: a closed-market probe must never count as proof of live SIP."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import sip_sunday_check as sc  # noqa: E402

ET = ZoneInfo("America/New_York")


def test_closed_market_probe_never_passes():
    ok, why = sc.entitlement_passes(3.0, False, None)
    assert not ok and "proves nothing" in why


def test_open_market_needs_a_fresh_print():
    assert sc.entitlement_passes(4.0, True, None)[0]
    assert not sc.entitlement_passes(900.0, True, None)[0]      # delayed plan
    assert not sc.entitlement_passes(None, True, None)[0]
    assert not sc.entitlement_passes(2.0, True, "subscription does not permit querying recent SIP data")[0]


def test_market_open_window_and_weekend():
    assert sc.market_open(datetime(2026, 11, 6, 10, 0, tzinfo=ET))      # Friday
    assert sc.market_open(datetime(2026, 11, 6, 17, 0, tzinfo=ET))      # after-hours counts (SIP bought at the end of the day)
    assert not sc.market_open(datetime(2026, 11, 6, 19, 30, tzinfo=ET))
    assert sc.market_open(datetime(2026, 11, 9, 8, 0, tzinfo=ET))       # Monday premarket counts (Saturday purchase)
    assert not sc.market_open(datetime(2026, 11, 9, 6, 30, tzinfo=ET))  # too early: prints are sparse
    assert not sc.market_open(datetime(2026, 11, 8, 20, 0, tzinfo=ET))  # Sunday night


def test_stamp_must_be_a_recent_pass():
    now = 1_800_000_000.0
    assert not sc.stamp_ok(None, now)[0]
    assert not sc.stamp_ok({"passed": False, "ts": now}, now)[0]
    assert sc.stamp_ok({"passed": True, "ts": now - 86400}, now)[0]
    assert not sc.stamp_ok({"passed": True, "ts": now - 6 * 86400}, now)[0]


def test_volume_cache_rule_iex_era_file_is_stale_after_the_flip(tmp_path):
    # pure date logic mirrored from the checklist: a cache dated before the first SIP session is IEX-built
    def stale(vdate, first_sip_day):
        return bool(first_sip_day) and bool(vdate) and vdate < first_sip_day
    assert stale("2026-11-06", "2026-11-09") is True
    assert stale("2026-11-09", "2026-11-09") is False
    assert stale("", "2026-11-09") is False and stale("2026-11-06", "") is False
