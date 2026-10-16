"""Calendar-flow window logic (prereg amended_1). Pure; no data."""
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


def test_nyse_schedule():
    h24 = C.nyse_holidays(2024)
    assert {date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29), date(2024, 5, 27),
            date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28), date(2024, 12, 25)} == h24
    assert date(2021, 12, 31) not in C.nyse_holidays(2022) and date(2021, 12, 31) not in C.nyse_holidays(2021)
    assert date(2020, 7, 3) in C.nyse_holidays(2020)              # July 4 on a Saturday -> Friday
    assert date(2001, 9, 11) not in C.nyse_holidays(2001)         # unscheduled closure is not a holiday
    assert not any(d.month == 6 and d.day in (18, 19, 20) for d in C.nyse_holidays(2021))   # Juneteenth from 2022


def test_windows_exclude_month_end_and_unscheduled_closures():
    days = _weekdays(date(2024, 3, 1), date(2024, 4, 10), closed={date(2024, 3, 29)})   # Good Friday
    f = C.window_flags(days)
    tom = {d for d, x in zip(days, f["TOM"]) if x}
    assert tom == {date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5), date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)}   # last day of March excluded
    qe = {d for d, x in zip(days, f["QE"]) if x}
    assert qe == {date(2024, 3, 27)}                                                    # 2nd-to-last only
    pre = {d for d, x in zip(days, f["PREHOL"]) if x}
    assert pre == {date(2024, 3, 28)}
    none = {d for d, x in zip(days, f["NONE"]) if x}
    assert date(2024, 3, 28) not in none and date(2024, 3, 26) in none
    sept = _weekdays(date(2001, 9, 4), date(2001, 9, 21), closed={date(2001, 9, d) for d in (11, 12, 13, 14)})
    f2 = C.window_flags(sept)
    assert not f2["PREHOL"][sept.index(date(2001, 9, 10))]                               # 9/11 is not a holiday


def test_opex_and_tax_january():
    days = _weekdays(date(2023, 12, 20), date(2024, 1, 31), closed={date(2023, 12, 25), date(2024, 1, 1), date(2024, 1, 15)})
    f = C.window_flags(days)
    tj = {d for d, x in zip(days, f["TAXJAN"]) if x}
    assert tj == {date(2023, 12, 27), date(2023, 12, 28), date(2023, 12, 29),
                  date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)}
    opex = {d for d, x in zip(days, f["OPEX"]) if x}
    assert {date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18), date(2024, 1, 19)} <= opex   # 3rd Fri 19 Jan


def test_score_cell_one_difference_per_year():
    days = _weekdays(date(2010, 1, 1), date(2021, 12, 31))
    flag = [d.day <= 7 for d in days]
    none = [not x for x in flag]
    rets = [0.0] + [(0.001 if flag[k] else 0.0) for k in range(1, len(days))]
    r = C.score_cell(days, rets, flag, none, None, None)
    assert r["years"] == 12 and abs(r["diff_bp"] - 10.0) < 1e-9 and not r["underpowered"]
    short = C.score_cell(days, rets, flag, none, date(2015, 1, 1), date(2018, 1, 1))
    assert short["underpowered"]                                                       # 3 years < 8
