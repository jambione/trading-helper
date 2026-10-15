"""Weekly undervalued-trending book: screen, agreement, order day, schedule, score. Pure; no network, no broker."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import overnight_book as OB
import weekly_book as WB

ET = ZoneInfo("America/New_York")


def _keys(n=66):
    d0 = date(2026, 7, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _by(keys, last=20.0, start=15.0, vol=2_000_000):
    """A steady climb from start to last: above its 50-session average and above 20/60 sessions ago."""
    n = len(keys)
    return {k: (start + (last - start) * i / (n - 1), vol) for i, k in enumerate(keys)}


def _row(**kw):
    keys = _keys()
    r = WB.trend_row(_by(keys, **kw), keys)
    assert r is not None
    return r


def test_screen_accepts_a_passing_fixture_and_rejects_each_rule():
    good = _row()
    assert WB.screen_reason(good, 9.5) is None
    assert WB.screen_reason({**good, "last_close": 4.99}, 9.5) == "price_below_5"
    assert WB.screen_reason({**good, "last_close": 50.0}, 9.5) == "price_50_or_more"
    assert WB.screen_reason({**good, "adv20": 19_999_999}, 9.5) == "adv_below_20m"
    assert WB.screen_reason({**good, "adv_present": 14}, 9.5) == "adv_history_short"
    assert WB.screen_reason(good, None) == "pe_missing"
    assert WB.screen_reason(good, 0) == "pe_not_positive"
    assert WB.screen_reason(good, -3) == "pe_not_positive"
    assert WB.screen_reason(good, 13) == "pe_above_12"
    assert WB.screen_reason({**good, "sma50": good["last_close"] + 1}, 9.5) == "below_sma50"
    assert WB.screen_reason({**good, "close_60": good["last_close"] + 1}, 9.5) == "down_over_60"
    assert WB.screen_reason({**good, "close_20": good["last_close"] + 1}, 9.5) == "down_over_20"


def test_universe_filters_drop_warrants_and_funds():
    from ticker_filters import is_common
    assert not is_common("ABCDW")                       # warrant
    assert OB.FUNDISH.search("iShares Core S&P 500 ETF")  # fund names stay out


def test_trend_row_needs_history_and_a_bar_yesterday():
    keys = _keys()
    by = _by(keys)
    assert WB.trend_row(by, keys[:40]) is None           # too short for the 60-session test
    del by[keys[-1]]
    assert WB.trend_row(by, keys) is None                # no bar on the last session: not trading


def test_agreement_is_the_intersection_ranked_by_sum_capped_and_never_padded():
    screen = {"AAA", "BBB", "CCC", "DDD", "EEE"}
    agy = WB.parse_picks('{"picks": [{"sym": "AAA", "rank": 1, "tag": "none", "why": "x"},'
                         '{"sym": "BBB", "rank": 2, "tag": "falling", "why": "x"},'
                         '{"sym": "ZZZ", "rank": 3, "tag": "none", "why": "not on the screen"},'
                         '{"sym": "CCC", "rank": 4, "tag": "none", "why": "x"}]}', screen)
    grok = WB.parse_picks('noise {"picks": [{"sym": "CCC", "rank": 1, "tag": "none"},'
                          '{"sym": "AAA", "rank": 5, "tag": "dilution"}, {"sym": "EEE", "rank": 2, "tag": "none"}]}',
                          screen)
    assert [p["sym"] for p in agy] == ["AAA", "BBB", "CCC"]          # unknown symbol dropped
    book, one = WB.agree(agy, grok)
    assert [r["sym"] for r in book] == ["CCC", "AAA"]                # sums 5 and 6
    assert one == ["BBB", "EEE"]                                     # one-model names logged, not bought
    assert WB.agree(agy, grok, cap=1)[0][0]["sym"] == "CCC"
    assert WB.parse_picks("not json", screen) == [] and WB.parse_picks('{"picks": "x"}', screen) == []
    assert WB.agree([], grok)[0] == []


def test_order_day_is_the_last_session_of_the_week_holiday_aware():
    week = [date(2026, 10, 12) + timedelta(days=i) for i in range(5)] + [date(2026, 10, 19)]
    assert [WB.order_day(d, week) for d in week[:5]] == [False, False, False, False, True]
    holiday = [d for d in week if d != date(2026, 10, 16)]                # Friday shut
    assert WB.order_day(date(2026, 10, 15), holiday) and not WB.order_day(date(2026, 10, 14), holiday)
    assert not WB.order_day(date(2026, 10, 17), week)                     # not a session


def test_weekly_schedule_orders_only_on_the_order_day():
    op = datetime(2026, 10, 14, 9, 30, tzinfo=ET)
    cl = datetime(2026, 10, 14, 16, 0, tzinfo=ET)
    mid = [n for n, _ in OB.weekly_steps(op, cl, False)]
    assert mid == ["weekly_screen"]                                       # no sell, no buy, no catch-up
    fri = dict(OB.weekly_steps(op, cl, True))
    assert fri["weekly_rebalance"] == cl - timedelta(minutes=2)
    assert "sell" not in fri and "sell_topup" not in fri


def test_week_diff_keeps_the_overlap():
    sell, buy, keep = WB.week_diff({"AAA": 10, "BBB": 5}, ["BBB", "CCC"])
    assert sell == ["AAA"] and buy == ["CCC"] and keep == ["BBB"]
    assert WB.week_diff({}, ["X", "Y"]) == ([], ["X", "Y"], [])


def test_week_score_charges_entries_and_exits_only():
    c0 = {"A": 10.0, "B": 20.0}
    c1 = {"A": 11.0, "B": 20.0}
    sc = WB.week_score(c0, c1, ["A", "B"], entered=["A"], leaving=["B"])
    assert abs(sc["gross_bp"] - 500.0) < 1e-9 and abs(sc["cost_bp"] - 4.0) < 1e-9 and abs(sc["net_bp"] - 496.0) < 1e-9
    assert WB.week_score(c0, {"A": 11.0}, ["A", "B"], [], [])["missing"] == ["B"]


def test_old_rank_is_still_12_1_momentum():
    keys = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(OB.LOOK + 5)]
    up = {k: (10.0 * (1 + i / 100), 10_000_000) for i, k in enumerate(keys)}
    flat = {k: (10.0, 10_000_000) for k in keys}
    rows = OB.rank({"UP": up, "FLAT": flat}, keys)
    assert [r["sym"] for r in rows] == ["UP", "FLAT"] and rows[0]["mom"] > 0
