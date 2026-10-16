"""Calendar-flow window logic. Pure; no data."""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import calendar_flows as C  # noqa: E402


def _weekdays(a, b, closed=()):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_turn_of_month_and_quarter_end():
    days = _weekdays(date(2024, 3, 25), date(2024, 4, 10), closed={date(2024, 3, 29)})   # Good Friday
    f = C.window_flags(days)
    on = {d for d, x in zip(days, f["TOM"]) if x}
    assert date(2024, 3, 28) in on                      # last trading day of March
    assert {date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)} <= on and date(2024, 4, 4) not in on
    qe = {d for d, x in zip(days, f["QE"]) if x}
    assert qe == {date(2024, 3, 27), date(2024, 3, 28)}


def test_pre_holiday_and_opex():
    days = _weekdays(date(2024, 3, 11), date(2024, 4, 1), closed={date(2024, 3, 29)})
    f = C.window_flags(days)
    pre = {d for d, x in zip(days, f["PREHOL"]) if x}
    assert pre == {date(2024, 3, 28)}                   # Thursday before Good Friday
    opex = {d for d, x in zip(days, f["OPEX"]) if x}
    assert opex == set(_weekdays(date(2024, 3, 11), date(2024, 3, 15)))   # 3rd Friday = 15 Mar


def test_tax_january():
    days = _weekdays(date(2023, 12, 20), date(2024, 1, 15), closed={date(2023, 12, 25), date(2024, 1, 1)})
    f = C.window_flags(days)
    tj = {d for d, x in zip(days, f["TAXJAN"]) if x}
    assert tj == {date(2023, 12, 27), date(2023, 12, 28), date(2023, 12, 29),
                  date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)}


def test_score_cell_diff_and_cost():
    days = _weekdays(date(2024, 1, 1), date(2024, 1, 31))
    rets = [0.0] + [0.001 if k % 2 else -0.001 for k in range(1, len(days))]
    flag = [bool(k % 2) for k in range(len(days))]
    r = C.score_cell(days, rets, flag, None, None)
    assert r["diff_bp"] > 0 and r["round_trips"] > 0
    assert abs(r["strategy_net_pct"] - (sum(x for x, fl in zip(rets[1:], flag[1:]) if fl) - r["round_trips"] * 2e-4) * 100) < 1e-9
