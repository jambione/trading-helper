import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import wr_trend_trade as W  # noqa: E402


def test_ema_matches_pandas():
    x = np.array([np.nan, np.nan, 1.0, 2.0, 3.0, np.nan, 5.0])
    ref = pd.Series(x).ewm(span=7, adjust=False).mean().to_numpy()
    got = W.ema(x, 7)
    assert np.allclose(got, ref, equal_nan=True)
    x2 = np.array([-50.0, -60.0, np.nan, np.nan, -20.0, -30.0])
    assert np.allclose(W.ema(x2, 3), pd.Series(x2).ewm(span=3, adjust=False).mean().to_numpy(), equal_nan=True)


def test_grid_fills_short_gaps_not_overnight():
    prev = np.array([[958, 1, 1, 1, 10.0, 1], [959, 1, 1, 1, 10.5, 1]])
    today = np.array([[570, 1, 11, 9, 10.2, 1], [574, 1, 11, 9, 10.4, 1]])
    keys, H, L, C = W.grid(prev, today)
    assert keys == [958, 959, 10570, 10571, 10572, 10573, 10574]
    assert C[3] == C[2] == 10.2 and H[3] == 10.2


def test_entries_need_oversold_then_both_rising_and_reentry_rule():
    fast = {m: -90.0 for m in range(560, 960)}
    slow = {m: -85.0 for m in range(560, 960)}
    for k, m in enumerate(range(600, 612)):          # fast climbs out of the bottom, slow edges up
        fast[m] = -90.0 + 4 * k
        slow[m] = -85.0 + 0.5 * k
    for m in range(612, 960):
        fast[m], slow[m] = fast[611], slow[611]
    for m in range(615, 620):                         # turn down at 615+
        fast[m] = fast[611] - (m - 614)
    ts = W.entries(fast, slow)
    assert ts and ts[0] >= 603 and fast[ts[0]] > W.OS
    assert len(ts) == 1                               # no new oversold event after the exit: no re-entry
    u = W.exit_minute(fast, slow, ts[0] + 1)
    assert u is not None and u >= 615


def test_trade_uses_next_open_and_c2_exit():
    sip = {601: (100.0, 100.5), 602: (100.5, 101.0), 610: (101.0, 101.2)}
    tr = W.trade(sip, 600, 605)                       # exit fill at 606: no bar -> last close at or before
    assert tr["entry_min"] == 601 and abs(tr["gross"] - (101.0 / 100.0 - 1)) < 1e-12


def test_pct_r_matches_signals():
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    sig = importlib.import_module("signals")
    rng = np.random.default_rng(1)
    c = 100 + rng.standard_normal(200).cumsum()
    h, lo = c + 0.3, c - 0.3
    ref = sig.williams_pr(pd.Series(h), pd.Series(lo), pd.Series(c), 21).to_numpy()
    assert np.allclose(W.pct_r(h, lo, c, 21), ref, equal_nan=True)


def test_eod_exit_matches_controls():
    sip = {601: (100.0, 100.5), 959: (101.0, 102.0)}
    tr = W.trade(sip, 600, None)                      # no exit signal: close at or before 15:59
    assert tr["exit_min"] == 960 and abs(tr["gross"] - (102.0 / 100.0 - 1)) < 1e-12
    assert abs(W.ret_over(sip, 601, 960) - tr["gross"]) < 1e-12      # a control with the same span agrees
    assert W.exit_px(sip, 959, 601) == 101.0           # an exit fill AT 15:59 is that bar's open


def test_decisions_only_at_real_line_minutes():
    prev = np.array([[m, 10, 10.2, 9.8, 10.0, 1] for m in range(840, 960)], dtype=float)
    today = np.array([[m, 10, 10.2, 9.8, 10.0, 1] for m in range(570, 960, 3)], dtype=float)   # a bar every 3 min
    fast, slow = W.lines(prev, today)
    assert W.real(fast, 573) and not W.real(fast, 574) and 574 in fast   # 574 is a grid fill, not a decision minute
