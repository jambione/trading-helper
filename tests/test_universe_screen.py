"""A universe screen that peeks, or that grades a move it could not trade,
invents an edge just as efficiently as a catalyst screen reading tomorrow's
news.

Two properties carry everything here: eligibility instants are never
earlier than the moment the rule could actually fire, and the playability
bar is the pre-registered one rather than whatever the tape happened to
produce.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

us = pytest.importorskip("universe_screen")

DAY = "2026-08-20"


def _ts(h_et, m_et=0, day=DAY):
    """A UTC epoch for an ET wall-clock time on DAY (August = EDT, UTC-4)."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(hours=h_et + us.ET_OFFSET_H, minutes=m_et)).timestamp()


def _bar(h_et, m_et, o, h, l, c, day=DAY):  # noqa: E741
    t = _ts(h_et, m_et, day)
    d = datetime.fromtimestamp(t, timezone.utc)
    return {"t": t, "day": day, "hm": (d.hour, d.minute),
            "o": o, "h": h, "l": l, "c": c}


# ------------------------------------------------------------- eligibility

def test_premarket_eligibility_is_pushed_to_the_open():
    """The desk places market orders; pre-market takes limits only.

    Crediting a universe with a 07:12 headline's move would score a
    constraint as if it were opportunity.
    """
    out = us._clamp_to_rth(_ts(7, 12), DAY)
    assert us._et_hm(out) == (9, 30)


def test_an_rth_instant_is_left_exactly_alone():
    t = _ts(11, 5)
    assert us._clamp_to_rth(t, DAY) == t


def test_clamp_survives_a_malformed_day():
    t = _ts(7, 0)
    assert us._clamp_to_rth(t, "not-a-day") == t


def test_earliest_instant_wins_per_name_day():
    plan = us._earliest([(DAY, "TEM", 300.0), (DAY, "TEM", 100.0),
                         (DAY, "AAPL", 250.0)])
    assert plan[DAY]["TEM"] == 100.0
    assert plan[DAY]["AAPL"] == 250.0


# ------------------------------------------------------------- playability

def _score(mfe, mae, sessions=10, green=8):
    return {"median_mfe": mfe, "median_mae": mae,
            "mfe_over_mae": (mfe / mae) if mae else None,
            "sessions": sessions, "sessions_green": green, "verdict": "DRIFT"}


def test_the_pay_bar_is_two_round_trips_of_price():
    assert us.COST_PCT == pytest.approx(0.79, abs=0.01)
    assert us.PLAYABLE_MIN_MFE_PCT == pytest.approx(1.58, abs=0.02)


def test_a_clean_drift_too_small_to_pay_is_unplayable():
    """The failure mode this screen exists to catch.

    MFE/MAE of 2.0 looks like a strong edge. At 0.40% of price it cannot
    cover a 0.79% round trip, and trading it loses money on contact.
    """
    rows = [{"mfe": 0.40, "mae": 0.20, "net": 0.1, "day": DAY}] * 40
    p = us.playability(rows, _score(0.40, 0.20))
    assert p["verdict"] == "UNPLAYABLE"
    assert p["pay_x"] < 1.0
    assert "medMFE" in p["why"]


def test_a_big_range_with_no_direction_is_unplayable():
    rows = [{"mfe": 4.0, "mae": 4.0, "net": 0.0, "day": DAY}] * 40
    p = us.playability(rows, _score(4.0, 4.0))
    assert p["verdict"] == "UNPLAYABLE"
    assert "MFE/MAE" in p["why"]


def test_a_universe_carried_by_a_minority_of_sessions_is_unplayable():
    rows = [{"mfe": 4.0, "mae": 2.0, "net": 1.0, "day": DAY}] * 40
    p = us.playability(rows, _score(4.0, 2.0, sessions=10, green=5))
    assert p["verdict"] == "UNPLAYABLE"
    assert "green" in p["why"]


def test_clearing_every_gate_reads_playable():
    rows = [{"mfe": 4.0, "mae": 2.0, "net": 1.0, "day": DAY}] * 40
    p = us.playability(rows, _score(4.0, 2.0, sessions=10, green=8))
    assert p["verdict"] == "PLAYABLE"
    assert p["pay_x"] == pytest.approx(4.0 / us.COST_PCT)
    assert p["mfe_r"] == pytest.approx(4.0 / us.R_PCT_OF_PRICE)


def test_share_clearing_cost_counts_samples_not_the_median():
    rows = ([{"mfe": 2.0, "mae": 1.0, "net": 0.5, "day": DAY}] * 3
            + [{"mfe": 0.1, "mae": 1.0, "net": -0.5, "day": DAY}])
    p = us.playability(rows, _score(1.05, 1.0))
    assert p["pct_clearing_cost"] == pytest.approx(0.75)


def test_empty_rows_do_not_produce_a_verdict():
    assert us.playability([], _score(1.0, 1.0))["verdict"] == "EMPTY"


# ------------------------------------------------------------- gap_hold

def _gap_day(day, open_px, low_after, prev_close=10.0):
    """Prior session plus an RTH day with a controllable post-open low."""
    prev = [_bar(15, 0, prev_close, prev_close, prev_close, prev_close,
                 day="2026-08-19")]
    rth = [_bar(9, 30 + i, open_px, open_px + 0.1, open_px - 0.05, open_px,
                day=day) for i in range(5)]
    for i in range(5, 40):
        rth.append(_bar(9, 30 + i, open_px, open_px + 0.1,
                        low_after, open_px, day=day))
    return prev + rth


def test_gap_hold_admits_a_name_that_gapped_and_held():
    bars = {"TEM": _gap_day(DAY, 11.0, 10.98)}
    plan = us.build_gap_hold(bars, gap_pct=3.0)
    assert "TEM" in plan.get(DAY, {})


def test_gap_hold_rejects_a_name_that_lost_its_opening_range():
    bars = {"TEM": _gap_day(DAY, 11.0, 10.0)}   # breaks the OR low
    assert not us.build_gap_hold(bars, gap_pct=3.0).get(DAY)


def test_gap_hold_rejects_a_name_that_did_not_gap():
    bars = {"TEM": _gap_day(DAY, 10.1, 10.08)}  # +1%, under the 3% floor
    assert not us.build_gap_hold(bars, gap_pct=3.0).get(DAY)


def test_gap_hold_stamps_eligibility_at_confirmation_not_at_the_open():
    """Nothing about this rule is knowable at 09:30 — it needs 30 minutes."""
    bars = {"TEM": _gap_day(DAY, 11.0, 10.98)}
    ts = us.build_gap_hold(bars, gap_pct=3.0)[DAY]["TEM"]
    assert us._et_hm(ts) >= (10, 0)


def test_gap_hold_needs_a_prior_session_for_the_close():
    """One day of bars has no prior close, so the gap is unknowable."""
    only_today = [b for b in _gap_day(DAY, 11.0, 10.98) if b["day"] == DAY]
    assert not us.build_gap_hold({"TEM": only_today}, gap_pct=3.0)


# ------------------------------------------------------------- registry

def test_an_unknown_universe_resolves_empty_rather_than_guessing():
    class A:
        rvol, gap, news_age, refresh_news = 5.0, 3.0, 60.0, False
    assert us.resolve("nonsense", 5, A()) == {}
