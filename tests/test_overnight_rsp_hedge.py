import importlib.util
import os

import numpy as np
import pandas as pd

_p = os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "overnight_rsp_hedge.py")
_s = importlib.util.spec_from_file_location("overnight_rsp_hedge", _p)
H = importlib.util.module_from_spec(_s)
_s.loader.exec_module(H)


def test_trailing_beta_is_point_in_time():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.01, 600)
    y = 1.5 * x
    y[400:] = -3.0 * x[400:]             # regime flips at row 400
    b = H.trailing_beta(y, x)
    assert np.isnan(b[:200]).all()        # needs 200 prior pairs: rows 0..199 -> first beta at row 200
    assert np.isclose(b[200], 1.5)
    assert np.isclose(b[400], 1.5)        # row 400's own pair is not in its window
    assert not np.isclose(b[401], 1.5)


def test_trailing_beta_counts_only_valid_pairs():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.01, 300)
    y = 2.0 * x
    x[:60] = np.nan                       # only 192 valid pairs in rows 0..251
    b = H.trailing_beta(y, x)
    assert np.isnan(b[252])
    assert np.isclose(b[261], 2.0)        # rows 9..260 hold 201 valid pairs


def test_on_series_rules():
    on, nbad = H.on_series([101, 0, 120, np.nan], [100, 100, 100, 100], [1, 0, 0, 0])
    assert np.isclose(on[0], 0.02)        # dividend included
    assert np.isnan(on[1]) and np.isnan(on[2]) and np.isnan(on[3])   # open <= 0, |ON| > 15%, missing
    assert nbad == 3


def test_zero_years_rule():
    on = np.r_[np.zeros(30), np.full(70, 0.001), np.zeros(10), np.full(90, 0.001)]
    yrs = np.r_[np.full(100, 2004), np.full(100, 2005)]
    share, bad = H.zero_years(on, yrs)
    assert share[2004] == 0.3 and share[2005] == 0.1
    assert bad == {2004}


def _frame(n=700, seed=2, edge=3e-4):
    rng = np.random.default_rng(seed)
    rsp = rng.normal(0, 0.006, n)
    spy = 0.8 * rsp + rng.normal(0, 0.003, n)
    univ = 1.1 * rsp + rng.normal(0, 0.002, n)
    picks = univ + edge + rng.normal(0, 0.004, n)
    idx = pd.bdate_range("2005-01-03", periods=n)
    df = pd.DataFrame({"book": picks - H.COST, "picks": picks, "univ": univ, "rsp": rsp, "spy": spy}, index=idx)
    df["drop_year"] = False
    return H.hedge_frame(df)


def test_decomposition_is_an_identity():
    f = _frame()
    c = f[f["common"]]
    e = H.evaluate(c)
    d = e["decomposition_bp"]
    assert np.isclose(d["sum"], e["rsp_hedged"]["mean_bp"], atol=1e-2)


def test_common_set_excludes_dropped_years_and_warmup():
    f = _frame()
    assert not f["common"].iloc[:200].any()
    f2 = f.copy()
    f2["drop_year"] = f2.index.year == 2006
    f2 = H.hedge_frame(f2)
    assert not f2.loc[f2.index.year == 2006, "common"].any()
    # betas still use the dropped year's rows (point-in-time estimate unchanged)
    assert np.allclose(f2["h_rsp"].values, f["h_rsp"].values, equal_nan=True)


def _ev(**kw):
    base = {"rsp_hedged": {"mean_bp": 3.0, "t": 2.5, "mde_bp": 4.0}, "rsp_hedged_6bp": {"mean_bp": 1.0},
            "sd_rsp_hedged_bp": 50.0, "sd_spy_hedged_bp": 60.0, "r2_book_on_rsp": 0.6, "r2_book_on_spy": 0.5,
            "decomposition_bp": {"S2_minus_costs": 1.0}}
    for k, v in kw.items():
        if isinstance(v, dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def test_verdict_branches():
    B = _ev()
    assert H.verdict(_ev(), B, True)[0].startswith("PASS")
    assert H.verdict(_ev(), B, False)[0].startswith("UNPROVEN (data-source")
    assert H.verdict(_ev(rsp_hedged={"mde_bp": 5.5}), B, True)[0].startswith("UNPROVEN (period A underpowered")
    assert H.verdict(_ev(rsp_hedged={"t": 1.9}), B, True)[0].startswith("FAIL")
    assert H.verdict(_ev(rsp_hedged_6bp={"mean_bp": -0.1}), B, True)[0].startswith("FAIL")
    assert H.verdict(_ev(sd_rsp_hedged_bp=61.0), B, True)[0].startswith("FAIL")
    assert H.verdict(_ev(r2_book_on_rsp=0.4), B, True)[0].startswith("FAIL")
    assert H.verdict(_ev(decomposition_bp={"S2_minus_costs": -0.1}), B, True)[0].startswith("UNPROVEN (period A passes only")
    assert H.verdict(_ev(), _ev(rsp_hedged={"mean_bp": 0.0}), True)[0].startswith("UNPROVEN (period B")
