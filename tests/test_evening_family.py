import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import evening_family as ef


def test_vol_ratio_uses_mean_of_prior_15_and_needs_10_bars():
    v = np.full(40, 100.0); v[30] = 500.0
    assert abs(ef.vol_ratio(v, 30) - 5.0) < 1e-9
    w = v.copy(); w[16:28] = np.nan                        # only 3 prior bars present
    assert np.isnan(ef.vol_ratio(w, 30))


def test_first_true_restarts_only_at_kept_trigger():
    assert ef.first_true([10, 20, 39, 40, 70]) == [10, 40, 70]


def test_trade_and_missing_bar_skip():
    o = np.full(390, 100.0); c = np.full(390, 100.0); c[110] = 100.5
    assert abs(ef.trade(o, c, 100, 10, 6.0) - (50.0 - 6.0)) < 1e-9
    c2 = c.copy(); c2[110] = np.nan
    assert ef.trade(o, c2, 100, 10, 6.0) is None


def test_later_minutes_ignores_future_triggers():
    # a trigger at 175 (after the candidate minute 150) must NOT remove 150; a trigger at 140 (before) removes 140..150 (H=10)
    assert 150 in ef.later_minutes(100, 10, [100, 175])
    assert 150 not in ef.later_minutes(100, 10, [100, 140])
    assert min(ef.later_minutes(100, 10, [100])) == 112


def test_primary_pool_excludes_only_triggers_up_to_t_and_duplicates():
    trig = {"B": [90], "C": [110], "D": [60]}
    pool = ef.primary_pool(["A", "B", "C", "D", "E"], trig, 100, "A", exclude=("E",))
    assert "B" not in pool                      # triggered in [85, 100]
    assert "C" in pool                          # triggers at 110 is future information at t=100
    assert "D" in pool and "E" not in pool and "A" not in pool


def _rows(n_days=70, per_day=8, edge=0.0, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_days):
        d = f"2026-03-{1 + i // 3:02d}-{i:03d}"
        for k in range(per_day):
            ca = float(rng.normal(-6, 20))
            rows.append({"d": d, "net": ca + edge + float(rng.normal(0, 5)), "net_s": None, "ca": ca, "cb": ca + float(rng.normal(0, 3))})
    return rows


def test_null_rows_give_no_edge_and_a_real_edge_is_detected():
    null = ef.evaluate(_rows(edge=0.0))
    assert null["status"] in ("FAIL", "UNDERPOWERED") and null["status"] != "EDGE"
    real = ef.evaluate(_rows(edge=6.0))
    assert real["status"] in ("EDGE", "PROFITABLE")


def test_minute_grid_fills_flat_and_returns_offset():
    pc = np.linspace(10, 11, 200); ph = pc + 0.01; pl = pc - 0.01
    c = np.full(390, np.nan); h = c.copy(); l = c.copy()
    c[:5] = 11.0; h[:5] = 11.01; l[:5] = 10.99
    cc, hh, ll, off = ef.minute_grid(pc, ph, pl, c, h, l)
    assert off == 112 and len(cc) == 112 + 390
    assert cc[112 + 10] == 11.0 and hh[112 + 10] == 11.0       # a short gap is carried flat
    assert np.isnan(cc[112 + 100])                              # a gap longer than 15 minutes stays a gap (signals.MINUTE_GAP_FILL_MAX)


def test_presquare_requires_two_consecutive_bars_and_trigger_is_the_second():
    n = 112 + 390
    cc = np.concatenate([np.linspace(20, 30, 112 + 200), np.linspace(30, 29.5, 190)])
    hh, ll = cc + 0.02, cc - 0.02
    bars = ef.presquare_bars(cc, hh, ll, 112, 21)
    assert all(15 <= b <= 360 for b in bars)
    assert len(bars) == len(set(bars))


# ---------------- through the real scoring path ----------------
def _day(names, seed, surge=None, spy_vol=0.0003):
    """Synthetic session in the warehouse layout: SPY + names, random-walk closes, flat volume. surge = {(name): (t, jump, drift5)}."""
    rng = np.random.default_rng(seed)
    syms = ["SPY"] + list(names)
    n = len(syms)
    spy_r = rng.normal(0, spy_vol, 390)
    c = np.zeros((n, 390)); v = np.full((n, 390), 1000.0)
    for k, s in enumerate(syms):
        r = rng.normal(0, 0.0004, 390) + (spy_r if k == 0 else 0.0)
        if surge and s in surge:
            t, jump, drift = surge[s]
            r[t] += jump; v[k, t] = 8000.0
            r[t + 1:t + 6] += drift / 5
        c[k] = 50.0 * np.exp(np.cumsum(r)) if k else 400.0 * np.exp(np.cumsum(r))
    o = np.concatenate([c[:, :1], c[:, :-1]], axis=1)
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, "o": o, "h": c * 1.0002, "l": c * 0.9998, "c": c, "v": v}


def test_score_day_finds_a_planted_surge_and_draws_controls_without_future_information():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 100 + i) for i in range(20)]
    day = _day(names, 7, surge={"N0": (100, 0.006, 0.0040), "N1": (105, 0.006, 0.0040)})
    out, st = ef.score_day(day, hist[-1], hist, names, "2026-02-02", which=("S",))
    rows = [r for r in out["S"] if r["sym"] == "N0" and r["t"] == 100]
    assert rows, "planted surge on N0 at bar 100 not found"
    r = rows[0]
    assert r["net"] > 20.0                                    # +40 bp drift planted over 5 bars, less 6 bp cost, plus noise
    assert r["ca"] is not None and r["cb"] is not None
    # N1 surges at 105 (after t=100): it is NOT excluded from N0's pool (future information), so it can serve as a control
    trig = {"N1": [105], "N0": [100]}
    assert "N1" in ef.primary_pool(names, trig, 100, "N0")


def test_null_day_has_no_planted_net_and_counts_match_the_trigger_rule():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 200 + i) for i in range(20)]
    day = _day(names, 9)
    out, _ = ef.score_day(day, hist[-1], hist, names, "2026-02-03", which=("S",))
    big = [r for r in out["S"] if r["net"] > 20.0]
    assert len(big) <= max(1, len(out["S"]) // 4)             # no planted drift: large nets are rare


def test_planted_edge_through_score_day_moves_the_lift_by_the_planted_amount():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 300 + i) for i in range(20)]
    base = _day(names, 11, surge={"N0": (100, 0.006, 0.0)})
    plant = _day(names, 11, surge={"N0": (100, 0.006, 0.0040)})      # same noise, +40 bp drift over the next 5 bars
    ob, _ = ef.score_day(base, hist[-1], hist, names, "2026-02-04", which=("S",))
    op, _ = ef.score_day(plant, hist[-1], hist, names, "2026-02-04", which=("S",))
    rb = [r for r in ob["S"] if r["sym"] == "N0" and r["t"] == 100][0]
    rp = [r for r in op["S"] if r["sym"] == "N0" and r["t"] == 100][0]
    assert abs((rp["net"] - rb["net"]) - 40.0) < 4.0


# ---------------- review fixes: stress, VOID, H2H, canary harness, one-read guard ----------------
def test_e_stress_net_uses_the_larger_of_2x_cost_and_actual_spreads():
    assert abs(ef.e_stress_net(net=10.0, rt_bp=4.0, sp_entry=1.0, sp_exit=1.0) - (14.0 - 8.0)) < 1e-9     # 2x cost wins
    assert abs(ef.e_stress_net(net=10.0, rt_bp=4.0, sp_entry=6.0, sp_exit=5.0) - (14.0 - 11.0)) < 1e-9    # actual spreads win


def test_attach_e_stress_skips_trades_with_a_missing_quote():
    rows = [{"sym": "SPY", "d": "2026-03-02", "t": 100, "net": 5.0}, {"sym": "SPY", "d": "2026-03-02", "t": 200, "net": 5.0}]
    costs = {"SPY": {"rt_bp": 2.0}}
    sp = lambda etf, d, m: None if m == 570 + 201 else 1.0      # second trade's entry quote missing  # noqa: E731
    ef.attach_e_stress(rows, costs, sp)
    assert rows[0]["net_s"] is not None and rows[1]["net_s"] is None


def test_void_when_baseline_missing_or_control_far_from_it():
    ev = {"ca_mean": -6.0}
    assert ef.void_status(ev, [])[0] is True
    assert ef.void_status(ev, [{"net": -6.5}, {"net": -5.5}])[0] is False
    assert ef.void_status({"ca_mean": 2.0}, [{"net": -6.0}])[0] is True
    assert ef.void_status({"ca_mean": float("nan")}, [{"net": -6.0}])[0] is True


def test_h2h_needs_60_days_and_reports_direction():
    def rows(delta, n=70):
        return [{"d": f"2026-03-{1 + i // 3:02d}-{i:03d}", "net": 1.0 + delta + (i % 5) * 0.4, "ca": 0.0} for i in range(n)]
    assert ef.h2h(rows(0.0, 30), rows(0.0, 30))["status"] == "UNDERPOWERED"
    better = ef.h2h(rows(3.0), rows(0.0))
    assert better["status"] == "L=10 BETTER" and better["mean"] > 0
    assert ef.h2h(rows(0.0), rows(0.0))["status"] == "NOT BETTER"


def test_canary_verdict_flags_a_bad_planted_value_and_passes_a_good_one():
    good = {k: {"planted": [8.0] * 10, "ctrl_shift": [0.0] * 10, "null_day_mean": list(np.random.default_rng(1).normal(0, 2, 30)), "row_mismatch": 0} for k in ef.KEYS}
    assert ef.canary_verdict(good)[0] is True
    bad = {**good, "E": {**good["E"], "planted": [5.0] * 10}}
    ok, why = ef.canary_verdict(bad)
    assert ok is False and any("E: planted" in w for w in why)
    shifted = {**good, "L": {**good["L"], "ctrl_shift": [0.0, 0.9]}}
    assert ef.canary_verdict(shifted)[0] is False
    biased = {**good, "S": {**good["S"], "null_day_mean": [4.0 + x for x in good["S"]["null_day_mean"]]}}
    assert ef.canary_verdict(biased)[0] is False
    few = {**good, "R10": {**good["R10"], "null_day_mean": [0.1] * 4}}
    assert ef.canary_verdict(few)[0] is False                                  # a null canary needs >= 10 days
    mism = {**good, "L": {**good["L"], "row_mismatch": 3}}
    assert ef.canary_verdict(mism)[0] is False                                 # rows missing from the planted pass cannot hide a failure


def test_sample_k_is_without_replacement_and_capped():
    import random
    got = ef.sample_k(random.Random(1), list("abc"), 5)
    assert sorted(got) == ["a", "b", "c"]


def test_canary_through_the_real_path_for_S_and_R():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 400 + i) for i in range(20)]
    for h in hist:
        h["_date"] = "x"
    day = _day(names, 21, surge={"N0": (100, 0.006, 0.0), "N3": (150, 0.006, 0.0)})
    out1, _ = ef.score_day(day, hist[-1], hist, names, "2026-02-09", which=("S",))
    assert len(out1["S"]) >= 2
    inj = ef.triggers_of(out1)
    d2, _ = ef.plant_copy(day, None, out1, 8.0)
    out2, _ = ef.score_day(d2, hist[-1], hist, names, "2026-02-09", which=("S",), inject=inj)
    a = {(r["sym"], r["t"]): r for r in out1["S"]}
    b = {(r["sym"], r["t"]): r for r in out2["S"]}
    assert set(a) == set(b)
    assert all(abs((b[k]["net"] - a[k]["net"]) - 8.0) < 0.5 for k in a)
    assert all(abs(b[k]["ca"] - a[k]["ca"]) < 0.5 for k in a if a[k]["ca"] is not None)
    nul = ef.null_injection(out1, names, ef.ALL_ETF, "2026-02-09")
    assert sum(len(v) for v in nul["S"].values()) == len(out1["S"])           # same count and the same bars
    assert sorted(t for v in nul["S"].values() for t in v) == sorted(r["t"] for r in out1["S"])


def test_run_refuses_a_second_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "OUT", tmp_path)
    (tmp_path / "run.pkl").write_bytes(b"x")
    try:
        ef.run("run")
        assert False, "should have refused"
    except SystemExit as e:
        assert "ONE read" in str(e)


def _etf_day(seed, surge=None):
    """Synthetic ETF session: SPY + the 12 ETFs; SMH carries a shared factor that mapped stocks follow."""
    rng = np.random.default_rng(seed)
    syms = ["SPY"] + [x for x in ef.ALL_ETF if x != "SPY"]
    c = np.zeros((len(syms), 390)); v = np.full((len(syms), 390), 1000.0)
    spy_r = rng.normal(0, 0.0003, 390)
    for k, s_ in enumerate(syms):
        r = rng.normal(0, 0.0004, 390) + (spy_r if s_ == "SPY" else 0.0)
        if surge and s_ in surge:
            t, jump = surge[s_]
            r[t] += jump; r[t - 1] += jump / 2; v[k, t] = 9000.0
        c[k] = 100.0 * np.exp(np.cumsum(r))
    o = np.concatenate([c[:, :1], c[:, :-1]], axis=1)
    return {"syms": syms, "idx": {s_: i for i, s_ in enumerate(syms)}, "o": o, "h": c * 1.0002, "l": c * 0.9998, "c": c, "v": v}


def test_R_inject_uses_the_same_control_pool_as_the_normal_path():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 500 + i) for i in range(20)]
    day = _day(names, 31)
    for h in hist + [day]:
        h["_date"] = "x"
    # inject two R21 triggers; the pool must still be every eligible name (not just the two triggered stocks)
    out, st = ef.score_day(day, hist[-1], hist, names, "2026-02-10", which=("R",), inject={"R21": {"N0": [120], "N1": [200]}, "R10": {}})
    rows = out["R21"]
    assert len(rows) == 2 and all(r["ca"] is not None for r in rows)
    assert st.get("R21_removed_primary", 0) <= 2                              # only the other triggered stock is removed, not a tiny pool


def test_E_and_L_go_through_score_day_and_the_canary_inputs_match():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 600 + i) for i in range(20)]
    ehist = [_etf_day(700 + i) for i in range(20)]
    for k, (h, e) in enumerate(zip(hist, ehist)):
        h["_date"] = e["_date"] = f"d{k}"
    day = _day(names, 41)
    eday = _etf_day(42, surge={"SMH": (100, 0.004), "QQQ": (200, 0.004)})
    costs = {x: {"rt_bp": 2.0} for x in ef.ALL_ETF}
    out, st = ef.score_day(day, hist[-1], hist, names, "2026-02-11", eday, ehist, costs, 0.0020, which=("E",))
    assert {(r["sym"], r["t"]) for r in out["E"]} >= {("SMH", 100), ("QQQ", 200)}
    inj = ef.triggers_of(out)
    d2, e2 = ef.plant_copy(day, eday, out, 8.0)
    out2, _ = ef.score_day(d2, hist[-1], hist, names, "2026-02-11", e2, ehist, costs, 0.0020, which=("E",), inject=inj)
    a = {(r["sym"], r["t"]): r for r in out["E"]}
    b = {(r["sym"], r["t"]): r for r in out2["E"]}
    assert set(a) == set(b)
    assert all(abs((b[k]["net"] - a[k]["net"]) - 8.0) < 0.5 for k in a)
    # misaligned ETF history disables E/L and is counted, not silently run
    out3, st3 = ef.score_day(day, hist[-1], hist, names, "2026-02-11", eday, ehist[:-1], costs, 0.0020, which=("E",))
    assert out3["E"] == [] and st3.get("etf_history_incomplete_days") == 1


def test_L_pass_two_controls_do_not_depend_on_processing_order():
    trig = {"B": [90], "C": [100]}
    # B triggered in [t-15, t] for t=100 and C triggered AT t: both are excluded for A, whatever order they were processed in
    assert ef.primary_pool(["A", "B", "C", "D"], trig, 100, "A") == ["D"]
    assert ef.primary_pool(["A", "B", "C", "D"], dict(reversed(list(trig.items()))), 100, "A") == ["D"]


def test_planting_one_key_per_pass_keeps_controls_unchanged_with_S_R10_R21_at_the_same_minute():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 800 + i) for i in range(20)]
    for h in hist:
        h["_date"] = "x"
    day = _day(names, 51)
    inj = {"S": {"N0": [100]}, "R21": {"N1": [100]}, "R10": {"N2": [100]}}
    out1 = {}
    o, _ = ef.score_day(day, hist[-1], hist, names, "2026-02-12", which=("S", "R"), inject=inj)
    cd = ef.canary_day(day, hist[-1], hist, names, "2026-02-12", None, None, None, 0.0020, o, which=("S", "R"))
    for k in ("S", "R21", "R10"):
        assert len(cd[k]["planted"]) == 1 and abs(cd[k]["planted"][0] - 8.0) < 0.5, k
        assert cd[k]["ctrl_shift"] and max(cd[k]["ctrl_shift"]) < 0.5, k
        assert cd[k]["row_mismatch"] == 0, k


def test_plant_copy_plants_a_shared_bar_once_and_only_the_requested_key():
    names = ["N0", "N1"]
    day = _day(names, 61)
    out = {k: [] for k in ef.KEYS}
    out["S"] = [{"sym": "N0", "t": 100}]
    out["R10"] = [{"sym": "N0", "t": 95}]                         # R10 exit bar t+10 = 105 = S exit bar 100+5? no: 95+10 = 105 and 100+5 = 105 -> shared bar
    both, _ = ef.plant_copy(day, None, out, 8.0)
    only_s, _ = ef.plant_copy(day, None, out, 8.0, keys=("S",))
    i = day["idx"]["N0"]
    assert abs(both["c"][i, 105] / day["c"][i, 105] - 1.0008) < 1e-9     # planted once, not twice
    assert abs(only_s["c"][i, 105] / day["c"][i, 105] - 1.0008) < 1e-9
    none, _ = ef.plant_copy(day, None, out, 8.0, keys=("E",))
    assert (none["c"] == day["c"]).all()


def test_run_refuses_when_the_canary_sample_is_too_thin(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "OUT", tmp_path)
    (tmp_path / "count.json").write_text('{"E_threshold": 0.001, "canary_plan_ok": false, "canary_plan": {"E": {"triggers": 2, "days": 1}}}')
    try:
        ef.run("run")
        assert False, "should have refused"
    except SystemExit as e:
        assert "canary sample too thin" in str(e)


def test_a_missing_spy_in_the_etf_day_skips_E_and_L_and_is_counted():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 900 + i) for i in range(20)]
    day = _day(names, 71)
    eday = _etf_day(72)
    del eday["idx"]["SPY"]
    out, st = ef.score_day(day, hist[-1], hist, names, "2026-02-13", eday, [_etf_day(i) for i in range(20)], {x: {"rt_bp": 2.0} for x in ef.ALL_ETF}, 0.0020, which=("E", "L"))
    assert out["E"] == [] and out["L"] == [] and st.get("etf_history_incomplete_days") == 1


def test_a_trigger_whose_trade_is_skipped_still_excludes_itself_from_controls_in_the_planted_pass():
    """Canary bug (11/08): a trigger with a missing exit bar has no row, so it was absent from the injected trigger set and re-entered the control
    pool in the planted pass - controls moved by 100+ bp when only exits changed. The injected set must be the COMPLETE trigger set."""
    names = [f"N{i}" for i in range(6)]                          # tiny pool so every eligible name is drawn (k = 5)
    hist = [_day(names, 1000 + i) for i in range(20)]
    for h in hist:
        h["_date"] = "x"
    day = _day(names, 81, surge={"N1": (100, 0.008, 0.0100), "N0": (110, 0.006, 0.0)})
    day["c"][day["idx"]["N1"], 105] = np.nan                     # N1 triggers at 100 but its trade (exit bar 105) is missing -> no row
    o, _ = ef.score_day(day, hist[-1], hist, names, "2026-02-14", which=("S",))
    assert not [r for r in o["S"] if r["sym"] == "N1" and r["t"] == 100]          # skipped: no row
    assert [r for r in o["S"] if r["sym"] == "N0" and r["t"] == 110]
    cd = ef.canary_day(day, hist[-1], hist, names, "2026-02-14", None, None, None, 0.0020, o, which=("S",))
    assert cd["S"]["ctrl_shift"] and max(cd["S"]["ctrl_shift"]) < 0.5, cd["S"]["ctrl_shift"]
