from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import liquidity_shock_maker as lsm  # noqa: E402


def _synth_series(base_px=100.0, n_bars=390):
    c = np.full(n_bars, base_px, dtype=np.float64)
    o = np.full(n_bars, base_px, dtype=np.float64)
    h = np.full(n_bars, base_px * 1.0005, dtype=np.float64)
    l = np.full(n_bars, base_px * 0.9995, dtype=np.float64)  # noqa: E741
    v = np.full(n_bars, 1000.0, dtype=np.float64)
    return o, h, l, c, v


# 1. Trade-through required for passive entry and target
def test_trade_through_required_for_passive_entry_and_target():
    t = 100
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    p_lim = c[t] * (1.0 - 0.0002)         # 99.98
    tt_lim = p_lim * (1.0 - 0.0002)       # 99.960004

    # Entry tests:
    # A: touches p_lim but does not trade through
    l[t + 1] = p_lim
    assert lsm.maker_trade(o, h, l, c, t, H=10) == "UNFILLED"

    # B: trades below p_lim but not by 2 bp
    l[t + 1] = p_lim - 0.00001
    assert lsm.maker_trade(o, h, l, c, t, H=10) == "UNFILLED"

    # C: trades through p_lim by >= 2 bp -> fills
    l[t + 1] = tt_lim - 0.01
    res = lsm.maker_trade(o, h, l, c, t, H=10)
    assert isinstance(res, dict)
    assert res["entry_px"] == p_lim

    # Target tests:
    # entry_px is 99.98, target is entry_px * 1.0040 = 100.37992
    p_target = p_lim * 1.0040
    tt_target = p_target * 1.0002

    # Case A: On bar t+2, h[t+2] touches p_target without 2 bp trade-through -> does NOT exit at target
    h[t + 2] = p_target
    res_a = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_a["exit_reason"] != "target"
    assert res_a["exit_bar"] != t + 2

    # Case B: On bar t+2, h[t+2] trades through p_target by >= 2 bp -> exits at target
    h[t + 2] = tt_target + 0.01
    res_b = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_b["exit_reason"] == "target"
    assert res_b["exit_bar"] == t + 2
    assert abs(res_b["exit_px"] - p_target) < 1e-9


# 2. Intrabar conservative ordering: stop beats target, and no entry bar target
def test_intrabar_conservative_ordering_stop_beats_target_and_no_entry_bar_target():
    t = 100
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    p_lim = c[t] * (1.0 - 0.0002)
    p_stop = p_lim * (1.0 - 0.0100)
    p_target = p_lim * (1.0 + 0.0040)
    tt_target = p_target * 1.0002

    # Case A: On entry bar t+1, order fills AND high surges past target trade-through
    # Strict anti-peeking: target MUST NOT execute on entry bar t+1!
    l[t + 1] = p_lim * 0.9997  # fills
    h[t + 1] = tt_target + 0.05
    res_a = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_a["exit_bar"] != t + 1
    assert res_a["exit_reason"] != "target"

    # Case B: On entry bar t+1, order fills AND drops below catastrophic stop
    # Catastrophic stop DOES execute on bar t+1
    l[t + 1] = p_stop - 0.05
    h[t + 1] = tt_target + 0.05
    res_b = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_b["exit_bar"] == t + 1
    assert res_b["exit_reason"] == "stop"
    assert abs(res_b["exit_px"] - min(p_stop, o[t + 1])) < 1e-9

    # Case C: On bar k >= t+2 (e.g. t+3), BOTH stop and target are touched
    # Worst-case priority: stop MUST be taken, never target
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    l[t + 1] = p_lim * 0.9997  # fills cleanly on t+1
    l[t + 3] = p_stop - 0.05   # hits stop on t+3
    h[t + 3] = tt_target + 0.05  # also hits target on t+3
    res_c = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_c["exit_bar"] == t + 3
    assert res_c["exit_reason"] == "stop"


# Helper for synthetic multi-stock sessions
def _day(names, seed, drop=None, spy_vol=0.0003):
    """Synthetic session: SPY + names, random walks.
    drop = {sym: (t, drop_amount)} creates an air-pocket drop at bar t.
    """
    rng = np.random.default_rng(seed)
    syms = ["SPY"] + list(names)
    n = len(syms)
    spy_r = rng.normal(0, spy_vol, 390)
    c = np.zeros((n, 390), dtype=np.float64)
    v = np.full((n, 390), 2000.0, dtype=np.float64)  # $50 * 2000 * 390 = $39M daily DV

    for k, s in enumerate(syms):
        r = rng.normal(0, 0.0004, 390) + (spy_r if k == 0 else 0.0)
        if drop and s in drop:
            t, drp = drop[s]
            r[t] -= drp
            r[t - 1] -= drp / 2
            v[k, t] = 10000.0  # volume spike
        c[k] = 50.0 * np.exp(np.cumsum(r)) if k else 400.0 * np.exp(np.cumsum(r))

    o = np.concatenate([c[:, :1], c[:, :-1]], axis=1)
    h = c * 1.0005
    l = c * 0.9995  # noqa: E741

    # Ensure open price at bar 0 is within [$25, $500]
    for k in range(n):
        o[k, 0] = c[k, 0]

    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, "o": o, "h": h, "l": l, "c": c, "v": v}


# 3. Causal prefix invariance: mutating t+1..389 cannot alter universe, beta, triggers at t, or control pool
def test_causal_prefix_invariance():
    names = [f"N{i}" for i in range(15)]
    hist = [_day(names, 100 + i) for i in range(20)]
    t = 100

    # Base day with air-pocket drop on N0 at t=100
    base_day = _day(names, 10, drop={"N0": (t, 0.0100)})

    # Clone day and mutate future bars t+1..389
    mut_day = {
        "syms": list(base_day["syms"]),
        "idx": dict(base_day["idx"]),
        "o": base_day["o"].copy(),
        "h": base_day["h"].copy(),
        "l": base_day["l"].copy(),
        "c": base_day["c"].copy(),
        "v": base_day["v"].copy(),
    }
    rng = np.random.default_rng(999)
    # Wild future mutations
    mut_day["c"][:, t + 1:] *= rng.uniform(0.5, 2.0, size=(len(base_day["syms"]), 390 - (t + 1)))
    mut_day["o"][:, t + 1:] *= rng.uniform(0.5, 2.0, size=(len(base_day["syms"]), 390 - (t + 1)))
    mut_day["v"][:, t + 1:] *= 50.0

    # 1. Beta invariance
    beta_base, _ = lsm.residual_stats(hist, names)
    beta_mut, _ = lsm.residual_stats(hist, names)
    assert beta_base == beta_mut

    # 2. Score both days
    out_base, st_base = lsm.score_day(base_day, hist[-1], hist, names, "2026-05-01", mode="run")
    out_mut, st_mut = lsm.score_day(mut_day, hist[-1], hist, names, "2026-05-01", mode="run")

    # Eligible names count must match
    assert st_base["eligible_names"] == st_mut["eligible_names"]

    # Trigger at bar t must match
    assert t in out_base["TRIG"]["M"].get("N0", [])
    assert t in out_mut["TRIG"]["M"].get("N0", [])

    # Primary control pool at bar t must be identical
    pool_base = lsm.primary_pool(names, out_base["TRIG"]["M"], t, "N0")
    pool_mut = lsm.primary_pool(names, out_mut["TRIG"]["M"], t, "N0")
    assert sorted(pool_base) == sorted(pool_mut)


# 4. Planted and null canaries pass, and skipped/unfilled triggers still excluded from controls
def test_planted_and_null_canaries_pass_and_skipped_or_unfilled_triggers_still_excluded_from_controls():
    names = [f"N{i}" for i in range(12)]
    hist = [_day(names, 200 + i) for i in range(20)]
    t = 100

    # N0 drops and fills, N1 drops at t=95 but does not trade through (unfilled)
    day = _day(names, 11, drop={"N0": (t, 0.0100), "N1": (95, 0.0100)})
    # Force N1 to not trade through on bar 96 (unfilled)
    n1_idx = day["idx"]["N1"]
    day["l"][n1_idx, 96] = day["c"][n1_idx, 95]  # no trade-through

    out1, st1 = lsm.score_day(day, hist[-1], hist, names, "2026-05-02", mode="run")
    assert any(r["sym"] == "N0" for r in out1["M"])
    assert not any(r["sym"] == "N1" for r in out1["M"])  # N1 unfilled, no trade row!
    assert 95 in out1["TRIG"]["M"].get("N1", [])        # But N1 is in TRIG!

    # Canary execution
    cd = lsm.canary_day(day, hist[-1], hist, names, "2026-05-02", lsm.PRIMARY_E_DROP, lsm.PRIMARY_VOL_MIN, out1)

    # (a) Planted +8 bp recovered
    assert len(cd["planted"]) > 0
    assert all(abs(p - 8.0) < 0.5 for p in cd["planted"])

    # (b) Controls do not shift
    assert cd["ctrl_shift"]
    assert all(cs < 0.5 for cs in cd["ctrl_shift"])

    # (c) Zero row mismatch
    assert cd["row_mismatch"] == 0

    # (d) Check that N1 (unfilled trigger at 95) was excluded from N0's primary control pool at t=100
    pool = lsm.primary_pool(names, out1["TRIG"]["M"], t, "N0")
    assert "N1" not in pool, "Unfilled trigger N1 at 95 must still be excluded from N0's pool at 100"


# 5. Run refuses second read
def test_run_refuses_second_read(tmp_path, monkeypatch):
    monkeypatch.setattr(lsm, "OUT", tmp_path)
    (tmp_path / "run.pkl").write_bytes(b"dummy_run_data")
    try:
        lsm.run("run")
        assert False, "Should have refused second read"
    except SystemExit as e:
        assert "run.pkl exists: the prereg allows ONE read" in str(e)


# 6. First_true refractory 30 bars restarts only at kept trigger
def test_first_true_refractory_30_bars():
    conds = [15, 20, 44, 45, 75, 76]
    kept = lsm.first_true(conds, refract=30)
    assert kept == [15, 45, 75]


# 7. Vol_ratio requires 10 bars present and matches mean of prior 15 bars
def test_vol_ratio_requires_10_bars():
    v = np.full(50, 100.0)
    v[30] = 300.0
    # Mean of bars 15..29 is 100.0 -> 300.0 / 100.0 = 3.0
    assert abs(lsm.vol_ratio(v, 30, k=15, need=10) - 3.0) < 1e-9

    # Less than 10 bars present -> NaN
    v_sparse = v.copy()
    v_sparse[15:25] = np.nan
    assert np.isnan(lsm.vol_ratio(v_sparse, 30, k=15, need=10))


# 8. Missing bars skips trade
def test_missing_bars_skips_trade():
    t = 100
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    p_lim = c[t] * (1.0 - 0.0002)
    l[t + 1] = p_lim * 0.9997  # fills

    # Missing open at t+1
    o_miss = o.copy()
    o_miss[t + 1] = np.nan
    assert lsm.maker_trade(o_miss, h, l, c, t, H=10) is None

    # Missing close at exit bar t+11
    c_miss = c.copy()
    c_miss[t + 11] = np.nan
    assert lsm.maker_trade(o, h, l, c_miss, t, H=10) is None

    # Missing bar intraday at t+5
    l_miss = l.copy()
    l_miss[t + 5] = np.nan
    assert lsm.maker_trade(o, h, l_miss, c, t, H=10) is None


# 9. Conservative gap-through entry never claims better price than p_lim
def test_conservative_gap_through_entry_never_claims_better_price():
    t = 100
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    p_lim = c[t] * (1.0 - 0.0002)  # 99.98
    p_stop = p_lim * (1.0 - 0.0100)

    # Bar t+1 gaps down below p_lim to 99.50
    o[t + 1] = 99.50
    l[t + 1] = 99.40
    res = lsm.maker_trade(o, h, l, c, t, H=10)
    assert isinstance(res, dict)
    # Entry price must NOT be credited as 99.50; it must strictly be p_lim = 99.98
    assert abs(res["entry_px"] - p_lim) < 1e-9

    # What if it gaps below the stop price to 98.00?
    o[t + 1] = 98.00
    l[t + 1] = 97.90
    res_gap_loss = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res_gap_loss["exit_reason"] == "stop"
    # Takes the full gap loss: exit at min(p_stop, o[t+1]) = 98.00
    assert abs(res_gap_loss["exit_px"] - 98.00) < 1e-9


# 10. Time stop at bar 11 exits with 3 bp taker fee
def test_time_stop_at_bar_11_takes_taker_half_cost():
    t = 100
    o, h, l, c, v = _synth_series(100.0)  # noqa: E741
    p_lim = c[t] * (1.0 - 0.0002)
    l[t + 1] = p_lim * 0.9997  # fills
    c[t + 11] = 100.20        # flat exit at t+11

    res = lsm.maker_trade(o, h, l, c, t, H=10)
    assert res["exit_reason"] == "time"
    assert res["exit_bar"] == t + 11
    # Net return = (100.20 / 99.98 - 1) * 1e4 - 3.0 bp
    expected_net = (100.20 / p_lim - 1.0) * 1e4 - 3.0
    assert abs(res["net"] - expected_net) < 1e-9
    assert abs(res["net_s"] - (expected_net - 4.0)) < 1e-9


# 11. VOID status when primary control differs from baseline by > 3 bp
def test_void_when_baseline_differs_from_primary_control_by_more_than_3bp():
    ev_pass = {"ca_mean": 2.5}
    base_pass = [{"net": 1.0}, {"net": 3.0}]  # mean 2.0 -> diff 0.5 <= 3.0
    is_void, bm = lsm.void_status(ev_pass, base_pass)
    assert is_void is False
    assert abs(bm - 2.0) < 1e-9

    ev_fail = {"ca_mean": 6.0}
    base_fail = [{"net": 1.0}, {"net": 2.0}]  # mean 1.5 -> diff 4.5 > 3.0
    is_void2, bm2 = lsm.void_status(ev_fail, base_fail)
    assert is_void2 is True


# 12. Report formats exit reason breakdown and hold duration
def test_report_formats_exit_reason_breakdown(tmp_path, monkeypatch):
    import pickle
    monkeypatch.setattr(lsm, "OUT", tmp_path)
    monkeypatch.setattr(lsm, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()

    # Create dummy run.pkl with all three exit reasons
    dummy_res = {
        "M": [
            {"d": "2026-05-01", "sym": "N0", "t": 100, "net": 40.0, "net_s": 36.0, "ca": 0.0, "cb": 0.0, "exit_bar": 104, "exit_reason": "target"},
            {"d": "2026-05-01", "sym": "N1", "t": 100, "net": -103.0, "net_s": -107.0, "ca": 0.0, "cb": 0.0, "exit_bar": 102, "exit_reason": "stop"},
            {"d": "2026-05-01", "sym": "N2", "t": 100, "net": 10.0, "net_s": 6.0, "ca": 0.0, "cb": 0.0, "exit_bar": 111, "exit_reason": "time"},
        ],
        "BASE": [{"net": 0.0}],
    }
    dummy_canary = {
        "planted": [8.0] * 10,
        "ctrl_shift": [0.0] * 10,
        "null_day_mean": [0.0] * 15,
        "row_mismatch": 0,
    }
    pickle.dump({
        "res": dummy_res,
        "stats": {"M_skipped": 0, "M_unfilled": 0, "M_filled": 3},
        "e_drop": 0.0080,
        "vol_min": 3.0,
        "canary": dummy_canary,
        "scored_days": 1,
    }, open(tmp_path / "run.pkl", "wb"))

    ret = lsm.report()
    assert ret == 0
    md = (tmp_path / "docs" / "LIQUIDITY_SHOCK_MAKER.md").read_text()
    assert "### Exit Reason Breakdown:" in md
    assert "TARGET: 1 of 3 (33.3%)" in md
    assert "STOP: 1 of 3 (33.3%)" in md
    assert "TIME: 1 of 3 (33.3%)" in md
    assert "Hold duration:" in md
