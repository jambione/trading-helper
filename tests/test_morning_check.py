"""The check that exists because everything else looked fine.

Its one job is to not lie in either direction. A column that has not
landed yet must not read PASS — pre-market rvol is legitimately absent,
and "too early to tell" reading as healthy is exactly how a broken
instrument survives a week. Equally, a genuinely empty result must not
read FAIL: the setup fires on ~5% of name-days, so zero setups is a
normal Monday.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

mc = pytest.importorskip("morning_check")

DAY = "2026-08-24"


def _ts(h, m=0):
    d = datetime.strptime(DAY, "%Y-%m-%d").replace(tzinfo=mc.ET)
    return (d + timedelta(hours=h, minutes=m)).timestamp()


@pytest.fixture(autouse=True)
def _clean():
    mc.RESULTS.clear()
    yield
    mc.RESULTS.clear()


def _status(name):
    for status, n, _ in mc.RESULTS:
        if n == name:
            return status
    return None


def _detail(name):
    for _, n, d in mc.RESULTS:
        if n == name:
            return d
    return ""


# ---------------------------------------------------------------- three-valued

def test_absent_data_is_pending_not_pass():
    """The property the whole file exists for."""
    mc.check_setup_fields(DAY, [])
    assert _status("setup fields present") == "PENDING"


def test_premarket_rvol_is_pending_not_fail():
    """The producer publishes rvol=None until its refresh resolves."""
    rows = [{"ts": _ts(5, 30), "symbol": "AAA"}]
    mc.check_rvol(DAY, rows, datetime.strptime(DAY, "%Y-%m-%d").replace(
        hour=5, minute=30, tzinfo=mc.ET))
    assert _status("rvol resolving") == "PENDING"


def test_rvol_still_absent_after_ten_is_a_failure():
    rows = [{"ts": _ts(11, 0), "symbol": "AAA", "rvol": None}] * 20
    mc.check_rvol(DAY, rows, datetime.strptime(DAY, "%Y-%m-%d").replace(
        hour=11, tzinfo=mc.ET))
    assert _status("rvol resolving") == "FAIL"


def test_rvol_present_after_ten_passes():
    rows = [{"ts": _ts(11, 0), "symbol": "AAA", "rvol": 6.0, "rvol_ok": True}] * 20
    mc.check_rvol(DAY, rows, datetime.strptime(DAY, "%Y-%m-%d").replace(
        hour=11, tzinfo=mc.ET))
    assert _status("rvol resolving") == "PASS"


def test_missing_float_before_ten_is_pending_not_fail():
    """10 symbols per 5-minute pass, and the watchlist churns daily."""
    rows = [{"ts": _ts(9, 35), "symbol": "NEW", "shares_out_m": None}]
    mc.check_float(DAY, rows, datetime.strptime(DAY, "%Y-%m-%d").replace(
        hour=9, minute=35, tzinfo=mc.ET))
    assert _status("float landing") == "PENDING"


def test_missing_float_after_ten_is_a_failure():
    rows = [{"ts": _ts(11, 0), "symbol": s, "shares_out_m": None}
            for s in ("A", "B", "C", "D")]
    mc.check_float(DAY, rows, datetime.strptime(DAY, "%Y-%m-%d").replace(
        hour=11, tzinfo=mc.ET))
    assert _status("float landing") == "FAIL"


# ---------------------------------------------------------------- empty is ok

def test_zero_setups_is_normal_not_a_failure():
    """It fires on ~5% of name-days. A quiet Monday is not a defect."""
    rows = [{"ts": _ts(11), "symbol": "AAA", "setup_ok": False,
             "setup_legs": "price,up", "setup_n_legs": 2}] * 5
    mc.check_setup_consistency(DAY, rows)
    assert _status("setup legs consistent") == "PASS"


def test_a_setup_marked_ok_without_five_legs_is_a_failure():
    rows = [{"ts": _ts(11), "symbol": "AAA", "setup_ok": True,
             "setup_legs": "price,up,rvol", "setup_n_legs": 3}]
    mc.check_setup_consistency(DAY, rows)
    assert _status("setup legs consistent") == "FAIL"


def test_a_genuine_setup_passes():
    rows = [{"ts": _ts(11), "symbol": "AAA", "setup_ok": True,
             "setup_legs": "float,news,price,rvol,up", "setup_n_legs": 5}]
    mc.check_setup_consistency(DAY, rows)
    assert _status("setup legs consistent") == "PASS"


# ---------------------------------------------------------------- the columns

def test_a_trader_predating_the_logging_is_caught():
    """The exact 8/22 failure: config armed, running code without it."""
    rows = [{"ts": _ts(11), "symbol": "AAA", "price": 5.0}] * 3
    mc.check_setup_fields(DAY, rows)
    assert _status("setup fields present") == "FAIL"
    assert "predates" in _detail("setup fields present")


def test_all_columns_emitted_passes():
    row = {"ts": _ts(11), "symbol": "AAA", "setup_ok": False,
           "setup_legs": "", "setup_n_legs": 0, "shares_out_m": None,
           "news_n_24h": 0, "rvol_ok": None, "vol_session": None,
           "pctr_both_rising": None, "pctr_diverging": None}
    mc.check_setup_fields(DAY, [row])
    assert _status("setup fields present") == "PASS"


def test_stage2_absent_is_a_warning_not_a_failure():
    """pctr_slow reaches 31-51% historically; that is a producer limit."""
    rows = [{"ts": _ts(11), "symbol": "A", "pctr_both_rising": None}] * 10
    mc.check_stage2(DAY, rows)
    assert _status("stage-2 timing") == "WARN"


def test_stage2_present_passes():
    rows = ([{"ts": _ts(11), "symbol": "A", "pctr_both_rising": True}] * 5
            + [{"ts": _ts(11), "symbol": "A", "pctr_both_rising": None}] * 5)
    mc.check_stage2(DAY, rows)
    assert _status("stage-2 timing") == "PASS"
