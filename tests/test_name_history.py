"""Tests for tools/studies/name_history.py (name_history + round_numbers preregs). Synthetic data, no network."""
from __future__ import annotations

import json
import random
import subprocess
import sys

import pytest

from _hist_synth import DAY, ROOT, T, FakeData, FakeStudyMarket, weekdays
import bars_structure as BS
import name_history as NH


# ------------------------------------------------------------------ quotes
def test_mid_at_staleness_rule():
    d = FakeData([DAY], quote_fn=lambda s, t: [t - 5.0, 99.0, 101.0])
    m, spr, age = NH.mid_at(d, "X", T(11, 0))
    assert m == 100.0 and abs(spr - 0.02) < 1e-12 and age == 5.0       # exactly 5 s old is fine
    d2 = FakeData([DAY], quote_fn=lambda s, t: [t - 5.1, 99.0, 101.0])
    assert NH.mid_at(d2, "X", T(11, 0)) is None
    d3 = FakeData([DAY], quote_fn=lambda s, t: None)
    assert NH.mid_at(d3, "X", T(11, 0)) is None


def test_outcome_costs_and_hedge():
    def q(sym, t):
        if sym == NH.SPY:
            return [t, 99.99, 100.01] if t < T(11, 10) else [t, 100.99, 101.01]     # SPY +1%
        return [t, 99.9, 100.1] if t < T(11, 10) else [t, 101.9, 102.1]            # name +2%
    d = FakeData([DAY], quote_fn=q)
    o = NH.outcome(d, "X", T(11, 0), beta=1.5)
    spread_e, spread_x = 0.2 / 100, 0.2 / 102
    assert abs(o["net30"] - 1e4 * (0.02 - 0.5 * spread_e - 0.5 * spread_x)) < 1e-6
    assert abs(o["hedged30"] - 1e4 * (0.02 - 1.5 * 0.01 - 0.5 * spread_e - 0.5 * spread_x)) < 1e-6
    so = NH.outcome(d, "X", T(11, 0), beta=1.5, short=True)
    assert abs(so["net30"] + 1e4 * (0.02 + 0.5 * spread_e + 0.5 * spread_x)) < 1e-6
    # stale exit quote drops the event
    d2 = FakeData([DAY], quote_fn=lambda s, t: [t, 99.9, 100.1] if t < T(11, 10) else [t - 9, 99.9, 100.1])
    assert NH.outcome(d2, "X", T(11, 0), 1.0) == {"drop": "stale_quote"}


# ------------------------------------------------------------------ same-name control
def test_control_minute_same_hour_far_from_t_deterministic():
    t = T(13, 45)
    m, tag = NH.control_minute("ABC", DAY, t)
    assert tag == "same" and BS.et_hour(m) == 13 and abs(m - t) > 30 * 60
    assert NH.control_minute("ABC", DAY, t) == (m, tag)
    # reproducible across processes (sha256 seed, not hash())
    code = (f"import sys; sys.path.insert(0, {repr(ROOT + '/tools/studies')}); sys.path.insert(0, {repr(ROOT)}); "
            f"import name_history as NH; print(NH.control_minute('ABC', {DAY!r}, {t!r})[0])")
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert float(out) == m


def test_control_minute_hour_fallbacks():
    m, tag = NH.control_minute("ABC", DAY, T(10, 30))      # hour 10: every minute within 30 min -> later hour
    assert tag == "later" and BS.et_hour(m) == 11
    m, tag = NH.control_minute("ABC", DAY, T(15, 0))       # hour 15 only 15:00; hour 16 none -> earlier (14)
    assert tag == "earlier" and BS.et_hour(m) == 14 and m <= T(14, 29)
    rng = random.Random(2)
    for _ in range(50):
        t = T(rng.randint(10, 14), rng.randint(0, 59))
        if BS.et_hm(t) < 10 * 60 + 30:
            continue
        m, tag = NH.control_minute("Z", DAY, t)
        assert T(9, 45) <= m <= T(15, 0) and abs(m - t) > 1800 and m + 5 + 1800 <= T(16, 0)


# ------------------------------------------------------------------ statistics
def _rows(n_days=40, n_names=30, diff=0.0, noise=10.0, seed=1, better_frac=0.5, hour_fx=True):
    rng = random.Random(seed)
    rows = []
    days = weekdays("2026-06-01", n_days)
    hv = NH.halves_of(days)
    for d in days:
        dfx = rng.gauss(0, 5)
        for k in range(n_names):
            h = 10 + (k % 5)
            b = rng.random() < better_frac
            y = dfx + (3 * h if hour_fx else 0) + (diff if b else 0) + rng.gauss(0, noise)
            rows.append({"day": d, "sym": f"N{k}", "hour": h, "half": hv[d], "g": b, "hedged30": y,
                         "net30": y, "tercile": "TOP" if b else "BOTTOM"})
    return rows


def test_fe_ols_mean_and_difference():
    rows = _rows(diff=20.0, noise=1.0)
    m = NH.tmean(rows, "hedged30")
    assert abs(m["coef"] - sum(r["hedged30"] for r in rows) / len(rows)) < 1e-9
    r = NH.fe_ols(rows, "hedged30", lambda r: 1.0 if r["g"] else 0.0)
    assert abs(r["coef"] - 20.0) < 0.5 and r["se"] > 0 and r["df"] == min(40, 30) - 1


def test_fe_ols_covariate_partialled_out():
    rng = random.Random(5)
    rows = []
    for i in range(600):
        g = i % 2 == 0
        z = rng.random() + (0.5 if g else 0.0)            # covariate correlated with the group
        rows.append({"day": f"d{i % 31}", "sym": f"s{i % 40}", "hour": 10, "band": "x",
                     "y": 10 * z + rng.gauss(0, 0.1), "z": z, "g": g})
    r = NH.fe_ols(rows, "y", lambda r: 1.0 if r["g"] else 0.0, ("day", "hour", "band"), ("z",))
    assert abs(r["coef"]) < 0.1                          # no group effect once z is controlled


def test_twoway_se_exceeds_iid_when_name_clustered():
    rng = random.Random(9)
    rows = []
    nfx = {f"s{k}": rng.gauss(0, 20) for k in range(20)}
    for d in range(40):
        for k in range(20):
            rows.append({"day": f"d{d}", "sym": f"s{k}", "y": nfx[f"s{k}"] + rng.gauss(0, 1)})
    se2 = NH.tmean(rows, "y")["se"]
    ys = [r["y"] for r in rows]
    iid = (sum((y - sum(ys) / len(ys)) ** 2 for y in ys) / (len(ys) - 1)) ** 0.5 / len(ys) ** 0.5
    assert se2 > 3 * iid


# ------------------------------------------------------------------ verdicts
BETTER = lambda r: r["g"]  # noqa: E731


def test_verdict_pass():
    rows = [dict(r, hedged30=r["hedged30"] + 50) for r in _rows(diff=30.0, noise=10.0)]
    v = NH.group_verdict(rows, "hedged30", BETTER, NH.Z_NH)
    assert v["verdict"] == "PASS", v["why"]


def test_verdict_fail_on_negative_t():
    v = NH.group_verdict(_rows(diff=-30.0, noise=10.0), "hedged30", BETTER, NH.Z_NH)
    assert v["verdict"] == "FAIL" and "<= -2" in v["why"][0]


def test_verdict_fail_when_powered_and_not_passed():
    v = NH.group_verdict(_rows(diff=0.0, noise=3.0, n_days=60, n_names=40), "hedged30", BETTER, NH.Z_NH)
    assert v["powered"] and v["verdict"] == "FAIL"
    assert all(h["mde"] <= NH.UNDER_BP for h in v["halves"].values())


def test_verdict_underpowered():
    v = NH.group_verdict(_rows(diff=0.0, noise=400.0, n_days=10, n_names=6), "hedged30", BETTER, NH.Z_NH)
    assert v["verdict"] == "UNDERPOWERED"


def test_verdict_better_group_must_be_positive():
    rows = [dict(r, hedged30=r["hedged30"] - 500) for r in _rows(diff=30.0, noise=10.0)]
    v = NH.group_verdict(rows, "hedged30", BETTER, NH.Z_NH)
    assert v["verdict"] != "PASS" and "better group's net <= 0" in v["why"]


def test_verdict_drop_top_names():
    rows = [dict(r, hedged30=r["hedged30"] + 50) for r in _rows(diff=-3.0, noise=2.0, n_names=30)]
    for r in rows:                                       # the whole difference comes from 5 names
        if r["sym"] in ("N0", "N1", "N2", "N3", "N4") and r["g"]:
            r["hedged30"] += 200
        if r["sym"] in ("N0", "N1", "N2", "N3", "N4") and not r["g"]:
            r["hedged30"] -= 20
    v = NH.group_verdict(rows, "hedged30", BETTER, NH.Z_NH)
    assert sorted(v["drops"]["sym"]["dropped"]) == ["N0", "N1", "N2", "N3", "N4"]
    assert v["verdict"] != "PASS" and "drop-top-sym test" in v["why"]


def test_verdict_drop_top_sessions():
    rows = [dict(r, hedged30=r["hedged30"] + 50) for r in _rows(diff=-3.0, noise=2.0, n_days=40)]
    days = sorted({r["day"] for r in rows})[:3]
    for r in rows:
        if r["day"] in days:
            r["hedged30"] += 300 if r["g"] else -40
    v = NH.group_verdict(rows, "hedged30", BETTER, NH.Z_NH)
    assert sorted(v["drops"]["day"]["dropped"]) == days and "drop-top-day test" in v["why"]


def test_name_selection_label_and_control_positive():
    rows = [dict(r, hedged30=r["hedged30"] + 50) for r in _rows(diff=30.0, noise=10.0)]
    for r in rows:
        r["htf_up"] = r["g"]
        r["ctrl"] = {"hedged30": r["hedged30"] + 5}        # event never beats its control
    v = NH.nh_hypothesis(rows, NH.H2_rows, NH.IS_HTF)
    assert v["verdict"] == "PASS" and v["label"].startswith("NAME SELECTION")
    rng = random.Random(4)
    for r in rows:
        r["ctrl"] = {"hedged30": r["hedged30"] - 40 + rng.gauss(0, 3)}
    v = NH.nh_hypothesis(rows, NH.H2_rows, NH.IS_HTF)
    assert v["control"]["positive"] and v["label"].startswith("PASS")


def test_failed_data_drop_rates():
    rows = _rows()
    for i, r in enumerate(rows):
        r["htf_up"] = r["g"]
        if r["g"] and i % 10 == 0:
            r["drop"] = "stale_quote"
    dr = NH.drop_rates(rows, NH.IS_HTF)
    assert dr["failed_data"]


def test_rn_projection_reads_control_arm_only():
    rows = _rows()
    rng = random.Random(8)
    for r in rows:
        r["tercile"] = "TOP"
        r["rn"] = "ABOVE" if r["g"] else "BELOW"
        r["ctrl"] = {"hedged30": rng.gauss(0, 10)}
        r["hedged30"] = None                             # no event outcome exists
    p = NH.rn_projection(rows)
    for h in "AB":
        assert p[h]["projected_se"] > 0 and p[h]["projected_mde"] is not None
    assert p["powered"] in (True, False)


def test_rn_verdict_uses_frozen_projection_for_power():
    rows = _rows(diff=0.0, noise=3.0, n_days=60, n_names=40)
    for r in rows:
        r["rn"] = "ABOVE" if r["g"] else "BELOW"
        r["band"] = "20-50"
        r["log_brk"] = 0.001
    v = NH.rn_hypothesis(rows, {"powered": False})
    assert v["verdict"] == "UNDERPOWERED"
    v = NH.rn_hypothesis(rows, {"powered": True})
    assert v["verdict"] == "FAIL"


# ------------------------------------------------------------------ features
def test_pct_ranks_and_terciles():
    assert NH.pct_ranks([1, 2, 3, 4]) == [0.125, 0.375, 0.625, 0.875]
    assert NH.pct_ranks([5, 5]) == [0.5, 0.5]
    rows = [{"day": "d", "T1": x, "T2": x} for x in range(9)]
    NH.assign_scores(rows)
    assert [r["tercile"] for r in rows] == ["BOTTOM"] * 3 + ["MID"] * 3 + ["TOP"] * 3
    rows.append({"day": "d", "T1": None, "T2": 0.3})
    NH.assign_scores(rows)
    assert rows[-1]["trend_score"] is None and rows[-1]["tercile"] is None


def test_universe_top_n_raw_close_volume():
    raw = {"A": {"p": [0, 0, 0, 20.0, 100]}, "B": {"p": [0, 0, 0, 9.99, 1e9]}, "C": {"p": [0, 0, 0, 50.0, 100]},
           "D": {"p": [0, 0, 0, 10.0, 10]}}
    assert NH.universe(raw, ["A", "B", "C", "D"], "p", n=2) == ["C", "A"]


def test_split_detection():
    days = ["d1", "d2", "d3"]
    raw = {"X": {"d1": [0, 0, 0, 100.0, 1], "d2": [0, 0, 0, 50.0, 1], "d3": [0, 0, 0, 51.0, 1]}}
    adj = {"X": {"d1": [0, 0, 0, 50.0, 1], "d2": [0, 0, 0, 50.0, 1], "d3": [0, 0, 0, 51.0, 1]}}
    d = FakeData(days, daily=raw, daily_adj=adj)
    assert NH.split_in(d, "X", days)
    adj2 = {"X": {"d1": [0, 0, 0, 99.0, 1], "d2": [0, 0, 0, 49.6, 1], "d3": [0, 0, 0, 50.6, 1]}}   # dividends only
    raw2 = {"X": {"d1": [0, 0, 0, 100.0, 1], "d2": [0, 0, 0, 50.0, 1], "d3": [0, 0, 0, 51.0, 1]}}
    assert not NH.split_in(FakeData(days, daily=raw2, daily_adj=adj2), "X", days)


# ------------------------------------------------------------------ end to end: count reads no event outcome
@pytest.fixture()
def synth(tmp_path, monkeypatch):
    days = weekdays("2026-05-26", 26)
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    mkt = FakeStudyMarket(days, syms, seed=11)
    monkeypatch.setattr(NH, "TEST_HI", days[-1])
    monkeypatch.setattr(NH.BRO, "script_rev", lambda: "test")
    monkeypatch.setattr(NH, "BETA_MIN_PAIRS", 50)
    return mkt, tmp_path


def test_count_reads_no_event_outcome_then_score(synth, monkeypatch):
    mkt, work = synth
    res = NH.run("count", work=str(work), mkt=mkt)
    assert res["n_events"] > 0
    fz = json.load(open(work / "frozen_groups.json"))
    ev_times = {(g["sym"], g["t"] + NH.ENTRY_LAG + k) for g in fz["groups"] for k in (0, NH.HOLD15, NH.HOLD30)}
    asked = {(s, t) for s, t in mkt.quote_log if s != NH.SPY}
    assert not (asked & ev_times), "count read an event quote"
    assert any(s != NH.SPY for s, _ in mkt.quote_log)        # the control arm was read
    mkt.quote_log.clear()
    out = NH.run("score", work=str(work), mkt=mkt)
    assert {"H1", "H2", "RN", "info", "rn_info", "power"} <= set(out)
    for hyp in ("H1", "H2", "RN"):
        assert out[hyp]["verdict"] in ("PASS", "FAIL", "UNDERPOWERED")
    assert out["frozen_sha256"] == res["frozen_sha256"]
    # every group is exclusive and exhaustive
    for g in fz["groups"]:
        assert g["rn"] in ("ABOVE", "BELOW", "NEUTRAL")
    md = NH.render_report(out)
    assert "Power (before any mean)" in md


def test_score_refuses_without_frozen_groups(synth):
    mkt, work = synth
    with pytest.raises(SystemExit):
        NH.run("score", work=str(work), mkt=mkt)


# ------------------------------------------------------------------ review round 1 (reviewer A)
def test_drop_rates_count_quote_drops_only():
    """FF1: a beta_pairs drop is not a quote drop - it must not enter the FAILED-DATA rate."""
    rows = _rows()
    for i, r in enumerate(rows):
        r["htf_up"] = r["g"]
        if r["g"] and i % 10 == 0:
            r["drop"] = "beta_pairs"
    dr = NH.drop_rates(rows, NH.IS_HTF)
    assert not dr["failed_data"] and dr["better"] == 0.0


def test_failed_data_hypothesis_never_reads_pass():
    """FF1: H1/H2 set verdict_data FAILED-DATA like RN, and the verdict / report do not read PASS."""
    rows = _rows(diff=20.0, noise=1.0)
    for i, r in enumerate(rows):
        r["htf_up"] = r["g"]
        r["ctrl"] = {"hedged30": r["hedged30"] - 15.0}
        if r["g"] and i % 10 == 0:
            r["drop"] = "stale_quote"
    v = NH.nh_hypothesis(rows, NH.H2_rows, NH.IS_HTF)
    assert v["verdict_data"] == "FAILED-DATA" and v["verdict"] == "FAILED-DATA"
    assert v["verdict_if_read"] == "PASS" and "PASS" not in v.get("label", "")
