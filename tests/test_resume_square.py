"""The one resumption cell, its warm-up, and the verdict. No warehouse."""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import early_entry_honest as E  # noqa: E402
import resume_square as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tape(n=80, squares=(), fast_at=None, low_at=None, high_at=None):
    close = [100.0 + i * 0.4 for i in range(n)]
    high = [c + 0.5 for c in close]
    low = [c - 0.5 for c in close]
    fast = [-40.0] * n
    slow = [-40.0] * n
    sq = set(squares)
    for i in squares:
        fast[i] = -5.0
        slow[i] = -5.0
    for i in squares:
        if i > 0 and (i - 1) not in sq:
            fast[i - 1] = -30.0
    for i, v in (fast_at or {}).items():
        fast[i] = v
    for i, v in (low_at or {}).items():
        low[i] = v
    for i, v in (high_at or {}).items():
        high[i] = v
    minutes = [570 + i for i in range(n)]
    return minutes, fast, slow, high, low, close


def test_first_square_of_the_day_does_not_fire():
    assert S.resume_onsets(*_tape(squares=(50,))) == []


def test_a_between_bar_at_minus_60_does_not_fire():
    assert S.resume_onsets(*_tape(squares=(30, 50), fast_at={40: -60.0})) == []
    assert S.resume_onsets(*_tape(squares=(30, 50), fast_at={40: -50.0})) == []


def test_valid_shallow_pullback_fires():
    assert S.resume_onsets(*_tape(squares=(30, 50))) == [620]


def test_a_bar_that_undercuts_the_pullback_low_does_not_fire():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    lo[50] = min(lo[31:50]) - 0.01
    assert S.resume_onsets(m, f, s, h, lo, c) == []


def test_a_low_that_only_touches_the_pullback_low_still_fires():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    lo[50] = min(lo[31:50])
    assert S.resume_onsets(m, f, s, h, lo, c) == [620]


def test_close_must_be_above_the_pullback_low():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    for k in range(31, 50):
        lo[k] = 500.0
    lo[50] = 500.0
    assert S.resume_onsets(m, f, s, h, lo, c) == []


def test_refractory_keeps_the_first_of_two_close_fires():
    fired = S.resume_onsets(*_tape(squares=(30, 50, 58)))
    assert fired == [620]
    scanned = S.scan(*_tape(squares=(30, 50, 58)))
    assert 570 + 58 in scanned["structural"]
    assert 570 + 58 not in S.other_onset_minutes(scanned)


def test_a_fire_fifteen_minutes_later_is_kept():
    assert S.resume_onsets(*_tape(squares=(30, 50, 65))) == [620, 635]


def test_window_bounds():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    for minute, kept in ((9 * 60 + 49, False), (9 * 60 + 50, True),
                         (15 * 60 + 30, True), (15 * 60 + 31, False)):
        m[50] = minute
        assert S.resume_onsets(m, f, s, h, lo, c) == ([minute] if kept else [])


def test_equal_highs_do_not_fire():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    for i in range(50 - 41, 51):
        h[i] = 100.0
    assert S.resume_onsets(m, f, s, h, lo, c) == []


def test_mismatched_lengths_do_not_fire():
    m, f, s, h, lo, c = _tape(squares=(30, 50))
    assert S.resume_onsets(m, f, s, h, lo, c[:-1]) == []


def test_ema_matches_the_published_adjust_false_formula():
    close = [10.0, 11.0, 12.5, 12.0, 13.0, 14.0, 13.5, 15.0, 15.5, 16.0]
    for span in (S.EMA_SHORT, S.EMA_LONG):
        got = S.ema_series(close, span)
        exp = pd.Series(close).ewm(span=span, adjust=False).mean().tolist()
        assert all(abs(a - b) < 1e-12 for a, b in zip(got, exp))
    text = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert '"ema_short": 8' in text
    assert '"ema_long":  21' in text


def _frame(day, n, price):
    start = pd.Timestamp(f"{day} 13:30", tz="UTC")
    return pd.DataFrame({
        "time": [start + pd.Timedelta(minutes=i) for i in range(n)],
        "open": [price] * n,
        "high": [price + 0.1] * n,
        "low": [price - 0.1] * n,
        "close": [price] * n,
        "volume": [100.0] * n,
    })


def test_warmed_series_tails_the_prior_and_drops_yesterday():
    seen = {}
    real = S.tail_prior

    def wrapped(frame):
        seen["n"] = len(frame)
        return real(frame)

    S.tail_prior = wrapped
    old = E.WARM_LINES
    E.WARM_LINES = False
    try:
        ser = S.warmed_series(_frame("2026-05-04", 30, 20.0), _frame("2026-05-01", 300, 10.0))
    finally:
        S.tail_prior = real
        E.WARM_LINES = old
    assert seen["n"] == 300
    assert len(real(_frame("2026-05-01", 300, 10.0))) == E.CON_PRIOR_BARS
    assert ser is not None
    assert len(ser["close"]) == len(ser["fast"]) == len(ser["high"]) == len(ser["low"]) == 30
    assert set(ser["close"]) == {20.0}


def test_universe_is_the_three_feed_intersection():
    assert S.universe_names({"A": 1, "B": 1, "C": 1}, {"A": 1, "B": 1}, {"A": 1, "C": 1}) == ["A"]


def test_day_job_sets_the_flag_and_passes_the_prior_frame():
    seen = {}

    def fake_frames(path):
        return {"AAA": path}

    def fake_warmed(iex_frame, prior_frame):
        seen["flag"] = E.WARM_LINES
        seen["prior"] = prior_frame
        seen["iex"] = iex_frame
        raise RuntimeError("stop after the flag check")

    def boom(*_a, **_k):
        raise AssertionError("sample_names")

    old = (S.P._frames, S.warmed_series, S.P.sample_names, E.WARM_LINES)
    S.P._frames, S.warmed_series, S.P.sample_names, E.WARM_LINES = fake_frames, fake_warmed, boom, False
    try:
        row = S.day_job("2026-05-01", "2026-04-30")
    finally:
        S.P._frames, S.warmed_series, S.P.sample_names, E.WARM_LINES = old
    assert seen["flag"] is True
    assert "2026-04-30" in seen["prior"]
    assert "2026-05-01" in seen["iex"] and "/iex/" in seen["iex"]
    assert row["drops"]["indicator_fail"] == 1
    assert row["gross"] == []


def _block(n=200, days=20, gross=0.002, t_gross=3.0, lift=0.0008, t_lift=2.5, net3=0.0017, net10=0.001):
    return {"n": n, "days": days, "gross": gross, "t_gross": t_gross,
            "lift": lift, "t_lift": t_lift, "net3": net3, "net10": net10, "lift_days": days}


def test_verdict_thin_half_that_misses_pass_is_fail():
    misses = _block(lift=0.0002, t_lift=0.8, net10=0.0, net3=0.0007, gross=0.001, t_gross=1.2)
    thin = _block(n=50, days=4)
    assert S.verdict({"A": misses, "B": thin, "ALL": misses}) == "FAIL"


def test_verdict_stays_underpowered_when_the_powered_half_would_pass():
    good = _block()
    thin = _block(n=50, days=4)
    assert S.verdict({"A": good, "B": thin, "ALL": good}) == "UNDERPOWERED"


def test_window_pin_matches_the_prereg():
    prereg = json.load(open(os.path.join(ROOT, "docs", "studies", "resume_square_prereg.json"), encoding="utf-8"))
    assert f"{S.WIN_N} sessions {S.WIN_FIRST}..{S.WIN_LAST}" in prereg["data"]
    assert prereg["pass"].endswith("Half A ends 2026-07-23.")
    assert S.CUT == "2026-07-23"
    ok = [S.WIN_FIRST] + ["2026-06-15"] * (S.WIN_N - 2) + [S.WIN_LAST]
    S.require_window(ok)
    for bad in (ok[:-1], ["2026-04-27"] + ok[1:], ok[:-1] + ["2026-10-16"]):
        try:
            S.require_window(bad)
        except SystemExit as exc:
            assert "REFUSING" in str(exc)
            continue
        raise AssertionError(bad)
