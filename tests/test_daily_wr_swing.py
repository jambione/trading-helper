import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import daily_wr_swing as dw  # noqa: E402


def test_williams_r_hand_computation():
    H = np.array([[10.0], [11], [12], [11.5], [11.0]])
    L = np.array([[9.0], [10], [10.5], [10], [9.5]])
    C = np.array([[9.5], [10.5], [11.8], [10.2], [9.6]])
    wr = dw.williams_r(H, L, C, 3)
    assert np.isnan(wr[1, 0])
    # t=4: HH(2..4)=12, LL=9.5 -> -100*(12-9.6)/(2.5) = -96
    assert abs(wr[4, 0] - (-96.0)) < 1e-9


def test_williams_r_needs_finite_window_and_range():
    H = np.array([[10.0], [np.nan], [10], [10]])
    L = np.array([[10.0], [10], [10], [10]])
    C = np.array([[10.0], [10], [10], [10]])
    wr = dw.williams_r(H, L, C, 2)
    assert np.isnan(wr[1, 0]) and np.isnan(wr[2, 0]) and np.isnan(wr[3, 0])   # NaN in window / zero range


def test_rsi2_restarts_after_gap_and_matches_wilder():
    rng = np.random.default_rng(3)
    c = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, 80)))
    r = dw.rsi2(c[:, None], min_after_seed=1)[:, 0]
    up = dn = None
    for i in range(1, 80):
        d = c[i] - c[i - 1]
        g, lo = max(d, 0), max(-d, 0)
        up, dn = (g, lo) if up is None else (0.5 * g + 0.5 * up, 0.5 * lo + 0.5 * dn)
        exp = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
        assert abs(r[i] - exp) < 1e-9
    g = c.copy()
    g[40] = np.nan
    r20 = dw.rsi2(g[:, None])[:, 0]
    assert np.isnan(r20[40]) and np.isnan(r20[41 + 18]) and np.isfinite(r20[41 + 20])


def test_exit_rules():
    C = np.full(20, 10.0)
    ex = np.zeros(20, bool)
    ex[7] = True
    assert dw.exit_bar(4, ex, C) == ("open", 8)          # first qualifying close sells the next open
    ex[:] = False
    assert dw.exit_bar(4, ex, C) == ("open", 10)         # cap: open of t+6
    C2 = C.copy()
    C2[6] = np.nan
    assert dw.exit_bar(4, ex, C2) == ("close", 5)        # NaN bar during the hold: last close before it


def _panel(T=1900, N=160, seed=0, plant_bp=0.0, index_reversal=0.0):
    """Synthetic panel: market factor (optionally mean-reverting) with heterogeneous betas and idiosyncratic noise.
    plant_bp > 0 adds that return on the session after every %R10 <= -90 uptrend signal (a stock-specific snap-back)."""
    rng = np.random.default_rng(seed)
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2016-01-04", periods=T)]
    m = np.zeros(T)
    for t in range(1, T):
        m[t] = -index_reversal * m[t - 1] + rng.normal(0.0004, 0.01)
    beta = rng.uniform(0.4, 1.8, N)
    eps = rng.normal(0, 0.015, (T, N))
    lr = m[:, None] * beta[None, :] + eps
    C = 50 * np.exp(np.cumsum(lr, axis=0))
    if plant_bp:
        H0, L0 = C * 1.005, C * 0.995
        wr = dw.williams_r(H0, L0, C, 10)
        s200 = dw.sma(C, 200)
        sig = (wr <= -90) & (C > s200)
        bump = np.zeros((T, N))
        bump[2:][sig[:-2]] = plant_bp / 1e4          # open t+1 -> close t+2 carries the plant
        C = C * np.exp(np.cumsum(bump, axis=0))
    O = np.vstack([C[:1], C[:-1]])
    H, L = np.maximum(O, C) * 1.004, np.minimum(O, C) * 0.996
    spy = 400 * np.exp(np.cumsum(m))
    D = {"days": days, "syms": [f"S{j}" for j in range(N)], "O": O, "C": C,
         "spy_o": np.concatenate(([spy[0]], spy[:-1])), "spy_c": spy}
    v = np.full((T, N), 1e6)
    return dw.prepare(D, H, L, C, v)


def _verdict(D, name="WR10"):
    sig, ex, ctl, later, ec, _ = dw.variant(D, name)
    tr, _ = dw.simulate(D, sig, ex, ctl, *dw.VERDICT, entry_close=ec)
    return dw.verdict(tr)


def test_null_canary_beta_and_index_reversal_do_not_read_edge():
    v, A, B, P, checks = _verdict(_panel(seed=1, index_reversal=0.3))
    edge_keys = list(checks)[:6] if checks else []
    assert not (checks and all(checks[k] for k in edge_keys)), (v, P, checks)


def test_planted_snapback_reads_edge():
    v, A, B, P, checks = _verdict(_panel(seed=2, plant_bp=60.0))
    edge_keys = list(checks)[:6]
    assert all(checks[k] for k in edge_keys), (v, A, B, checks)


def test_controls_follow_the_nan_gap_rule():
    O = np.array([[10.0, 10], [10, 10], [11, 11], [12, 12], [13, 13]])
    C = np.array([[10.0, 10], [10.5, 10.5], [11.5, np.nan], [12.5, 12.5], [13.5, 13.5]])
    Cf = dw.ffill(C)
    r, fb = dw.leg_returns(O, C, Cf, [0, 1], 1, "open", 4)
    assert abs(r[0] - (13 / 10 - 1)) < 1e-12 and not fb[0]
    assert abs(r[1] - (10.5 / 10 - 1)) < 1e-12 and fb[1]      # NaN close at bar 2 -> sell at close of bar 1
    r2, _ = dw.leg_returns(O, C, Cf, [0], 1, "close", 3)
    assert abs(r2[0] - (12.5 / 10 - 1)) < 1e-12


def test_exit_bar_no_close_after_entry_is_skipped():
    C = np.array([10.0, 10, np.nan, 10, 10, 10, 10, 10])
    assert dw.exit_bar(1, np.zeros(8, bool), C) is None
