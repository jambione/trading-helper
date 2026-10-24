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
