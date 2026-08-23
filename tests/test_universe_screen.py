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


def _rows(mfe, mae, n=40, cost=None):
    r = {"mfe": mfe, "mae": mae, "net": mfe - mae, "day": DAY}
    if cost is not None:
        r["cost"] = cost
    return [dict(r) for _ in range(n)]


def test_the_fixed_pay_bar_is_two_round_trips_of_price():
    assert us.COST_PCT == pytest.approx(0.79, abs=0.01)
    assert us.PLAYABLE_MIN_MFE_PCT == pytest.approx(1.58, abs=0.02)


def test_a_clean_drift_too_small_to_pay_is_unplayable():
    """The failure mode this screen exists to catch.

    MFE/MAE of 2.0 looks like a strong edge. At 0.40% of price it cannot
    cover a 0.79% round trip, and trading it loses money on contact.
    """
    p = us.playability(_rows(0.40, 0.20), _score(0.40, 0.20))
    assert p["verdict"] == "UNPLAYABLE"
    assert p["pay_x"] < 1.0
    assert "medMFE" in p["why"]


def test_a_big_range_with_no_direction_is_unplayable():
    p = us.playability(_rows(4.0, 4.0), _score(4.0, 4.0))
    assert p["verdict"] == "UNPLAYABLE"
    assert "MFE/MAE" in p["why"]


def test_a_universe_carried_by_a_minority_of_sessions_is_unplayable():
    p = us.playability(_rows(4.0, 2.0), _score(4.0, 2.0, sessions=10, green=5))
    assert p["verdict"] == "UNPLAYABLE"
    assert "green" in p["why"]


def test_clearing_every_gate_reads_playable():
    p = us.playability(_rows(4.0, 2.0), _score(4.0, 2.0, sessions=10, green=8))
    assert p["verdict"] == "PLAYABLE"
    assert p["pay_x"] == pytest.approx(4.0 / us.COST_PCT)
    assert p["mfe_r"] == pytest.approx(4.0 / us.R_PCT_OF_PRICE)


def test_share_clearing_cost_counts_samples_not_the_median():
    rows = _rows(2.0, 1.0, n=3) + _rows(0.1, 1.0, n=1)
    p = us.playability(rows, _score(1.05, 1.0))
    assert p["pct_clearing_cost"] == pytest.approx(0.75)


def test_a_cheap_universe_is_charged_more_than_an_expensive_one():
    """The whole point of the per-name model.

    Identical excursion, different friction: the expensive names clear the
    bar and the cheap ones do not, and before 8/23 both read the same.
    """
    cheap = us.playability(_rows(1.6, 0.8, cost=1.5), _score(1.6, 0.8))
    rich = us.playability(_rows(1.6, 0.8, cost=0.6), _score(1.6, 0.8))
    assert cheap["verdict"] == "UNPLAYABLE"
    assert rich["verdict"] == "PLAYABLE"
    assert rich["pay_x"] > cheap["pay_x"]


def test_each_sample_is_charged_its_own_cost_not_the_pool_median():
    """A cheap name must not borrow an expensive name's spread."""
    rows = _rows(1.0, 0.5, n=2, cost=0.2) + _rows(1.0, 0.5, n=2, cost=5.0)
    p = us.playability(rows, _score(1.0, 0.5))
    assert p["pct_clearing_cost"] == pytest.approx(0.5)


def test_the_bar_is_a_multiple_so_cheapness_alone_cannot_pass():
    """Halving cost halves the bar; only the RATIO can clear it."""
    p = us.playability(_rows(0.40, 0.20, cost=0.20), _score(0.40, 0.20))
    assert p["bar_pct"] == pytest.approx(0.40)
    assert p["verdict"] == "PLAYABLE"
    thin = us.playability(_rows(0.30, 0.15, cost=0.20), _score(0.30, 0.15))
    assert thin["verdict"] == "UNPLAYABLE"


def test_rows_without_a_cost_fall_back_to_the_fixed_model():
    p = us.playability(_rows(4.0, 2.0), _score(4.0, 2.0, sessions=10, green=8))
    assert p["median_cost_pct"] == pytest.approx(us.COST_PCT)


# ------------------------------------------------------------- cost model

def test_one_tick_is_worth_a_hundred_times_more_on_a_cheap_stock():
    assert us.tick_spread_pct(2.0) == pytest.approx(0.50)
    assert us.tick_spread_pct(200.0) == pytest.approx(0.005)


def test_a_zero_price_cannot_be_assigned_a_finite_cost():
    assert us.tick_spread_pct(0.0) == float("inf")


def test_roll_recovers_a_spread_from_bid_ask_bounce():
    """Random buy/sell prints around a fixed mid are pure bounce, no news.

    The direction must be i.i.d., which is Roll's actual assumption — see
    the sibling test for what systematic alternation does to it.
    """
    import random
    rnd = random.Random(7)
    bars = []
    for i in range(360):                            # 0.10 wide on a $10 mid
        px = 10.0 + (0.05 if rnd.random() < 0.5 else -0.05)
        bars.append(_bar(9, 30 + i, px, px, px, px))
    est = us.roll_spread_pct(bars, DAY)
    assert est is not None
    assert est == pytest.approx(1.0, rel=0.15)


def test_perfectly_alternating_prints_inflate_roll_twofold():
    """A known limit, pinned so nobody rediscovers it as a bug.

    Roll assumes trade direction is i.i.d. Strict alternation makes the
    serial covariance -S^2 instead of -S^2/4, so the estimate doubles.
    Real tape sits between the two, which is why the tick floor and the
    quote validation both exist.
    """
    bars = []
    for i in range(60):
        px = 10.0 + (0.05 if i % 2 else -0.05)      # true spread 1.0%
        bars.append(_bar(9, 30 + i, px, px, px, px))
    assert us.roll_spread_pct(bars, DAY) == pytest.approx(2.0, rel=0.1)


def test_roll_declines_to_guess_on_a_trending_tape():
    """Positive serial covariance is drift, not bounce. None, not zero."""
    bars = [_bar(9, 30 + i, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1,
                 10 + i * 0.1) for i in range(60)]
    assert us.roll_spread_pct(bars, DAY) is None


def test_roll_refuses_a_sample_too_short_to_mean_anything():
    bars = [_bar(9, 30 + i, 10, 10, 10, 10) for i in range(5)]
    assert us.roll_spread_pct(bars, DAY) is None


def test_cost_falls_back_to_the_tick_floor_and_says_so():
    """Trending tape gives Roll nothing, so the floor must be labelled."""
    bars = [_bar(9, 30 + i, 10 + i * 0.1, 10 + i * 0.1, 10 + i * 0.1,
                 10 + i * 0.1) for i in range(60)]
    cost, src = us.name_cost_pct(bars, DAY, "measured")
    assert src == "tick"
    assert cost > us.GIVE_PCT


def test_the_fixed_model_ignores_the_name_entirely():
    bars = [_bar(9, 30 + i, 2.0, 2.0, 2.0, 2.0) for i in range(60)]
    assert us.name_cost_pct(bars, DAY, "fixed") == (us.COST_PCT, "fixed")


def test_measured_cost_never_dips_below_give_plus_one_tick():
    """Roll can under-read; the tick is arithmetic and cannot be argued with."""
    bars = []
    for i in range(60):
        px = 3.0 + (0.0001 if i % 2 else -0.0001)   # implausibly tight
        bars.append(_bar(9, 30 + i, px, px, px, px))
    cost, _ = us.name_cost_pct(bars, DAY, "measured")
    assert cost >= us.GIVE_PCT + us.tick_spread_pct(3.0) - 1e-9


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
