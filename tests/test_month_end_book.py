"""month_end_book — calendar roles, the signal, and the plan score."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import month_end_book as me

ET = ZoneInfo("America/New_York")
# Oct 2026 tail and early Nov: Thu 29, Fri 30 (last session), Mon Nov 2
DAYS = [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30),
        date(2026, 11, 2), date(2026, 11, 3)]


def test_roles_pick_the_last_two_sessions_of_the_month():
    assert me.role(DAYS, date(2026, 10, 28)) is None
    assert me.role(DAYS, date(2026, 10, 29)) == "entry"
    assert me.role(DAYS, date(2026, 10, 30)) == "exit"
    assert me.role(DAYS, date(2026, 11, 2)) is None


def test_role_unknown_without_a_later_session_or_off_calendar():
    assert me.role(DAYS[:4], date(2026, 10, 30)) is None     # cannot see the month turn
    assert me.role(DAYS, date(2026, 10, 31)) is None          # Saturday


def test_signal_and_side_trade_against_the_month_winner():
    s = me.signal(100, 100, 103, 101)                          # stocks beat bonds by 2%
    assert abs(s - 0.02) < 1e-12
    assert me.side_for(s) == "short"
    assert me.side_for(me.signal(100, 100, 99, 101)) == "long"
    assert me.side_for(0.0) is None


def test_score_is_minus_sign_times_spy_move_less_cost():
    assert abs(me.score(0.02, 100.0, 99.0) - (100 - me.COST_BP)) < 1e-9     # short, SPY -1%
    assert abs(me.score(-0.02, 100.0, 99.0) - (-100 - me.COST_BP)) < 1e-9   # long, SPY -1%
    assert me.score(0.0, 100.0, 99.0) == 0.0


def test_schedule_market_mode_decides_before_entering():
    close = datetime(2026, 10, 29, 16, 0, tzinfo=ET)
    steps = dict(me.schedule("entry", close))
    assert steps["decide"] < steps["enter"] < close < steps["reconcile"]
    assert dict(me.schedule("exit", close))["exit"] == close - timedelta(minutes=2 if me.ORDER_MODE == "market" else 15)
