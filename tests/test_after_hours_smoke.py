"""After-hours smoke — offline half, no Alpaca."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import after_hours_smoke as smoke  # noqa: E402


def test_offline_checks_all_pass():
    checks = smoke.run_offline_checks()
    failed = [c.name for c in checks if not c.ok]
    assert failed == [], failed
    names = {c.name for c in checks}
    assert "observe_veto" in names
    assert "h4_sim_stop" in names
    assert "fill_truth_sell_time" in names


def test_rth_closed_on_weekend():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    sat = datetime(2026, 8, 22, 12, 0, tzinfo=et)  # Saturday
    assert smoke._rth_open(sat) is False
    fri_open = datetime(2026, 8, 21, 10, 0, tzinfo=et)
    assert smoke._rth_open(fri_open) is True
    fri_eve = datetime(2026, 8, 21, 17, 30, tzinfo=et)
    assert smoke._rth_open(fri_eve) is False
