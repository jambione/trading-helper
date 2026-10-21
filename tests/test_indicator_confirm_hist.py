"""Warehouse loading, PRE timing, control, V context and the count step for indicator_confirm_hist. No market data."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import bars_structure as BS  # noqa: E402
import indicator_confirm_hist as H  # noqa: E402
import indicator_confirm_study as S  # noqa: E402


def _npz(path, rows_by_sym):
    syms = sorted(rows_by_sym)
    idx, mins, o, h, lo, c, v = [], [], [], [], [], [], []
    for k, s in enumerate(syms):
        for m, px in rows_by_sym[s]:
            idx.append(k); mins.append(m); o.append(px); h.append(px + .01); lo.append(px - .01); c.append(px)
            v.append(100.0)
    np.savez(path, syms=np.array(syms), sym_idx=np.array(idx), minute=np.array(mins), o=np.array(o), h=np.array(h),
             l=np.array(lo), c=np.array(c), v=np.array(v))


def test_load_raw_sorts_and_keeps_rth(tmp_path, monkeypatch):
    (tmp_path / "sip").mkdir()
    _npz(tmp_path / "sip" / "2026-06-01.npz", {"BBB": [(600, 2.0), (570, 1.0), (500, 9.0)], "AAA": [(571, 3.0)]})
    monkeypatch.setattr(H, "WH", str(tmp_path))
    raw = H.load_raw("sip", "2026-06-01")
    assert list(raw["BBB"][:, 0]) == [570.0, 600.0]          # 08:20 dropped, sorted by minute
    assert H.sip_oc(raw["BBB"])[600] == (2.0, 2.0) and H.bars_dict(raw["AAA"])[571][3] == 3.0


def test_sample_is_seeded_and_capped():
    names = [f"N{i}" for i in range(1000)]
    a = H.sample_names("2026-06-01", names)
    assert len(a) == H.SAMPLE and a == H.sample_names("2026-06-01", list(reversed(names)))
    assert a != H.sample_names("2026-06-02", names)
    assert H.sample_names("2026-06-01", names[:5]) == sorted(names[:5])


def test_half_split_is_fixed():
    assert H.half_of("2026-07-29") == "A" and H.half_of("2026-07-30") == "B"


def test_pre_minutes_are_onset_plus_one_inside_the_window(monkeypatch):
    mins = list(range(9 * 60 + 50, 15 * 60 + 40))
    ser = {"minutes": mins, "fast": [0.0] * len(mins), "slow": [0.0] * len(mins)}
    fire_at = {9 * 60 + 58, 9 * 60 + 59, 10 * 60 + 5, 10 * 60 + 20, 15 * 60 + 29, 15 * 60 + 30}
    monkeypatch.setattr(H.E, "pre_flag", lambda f, s, i: mins[i] in fire_at)
    # 09:58 is an onset before the window and 09:59 only continues it (not an onset); 10:05 -> m 10:06;
    # 10:20 is 15 min after 10:05 -> m 10:21; 15:29 -> m 15:30; 15:30 continues the 15:29 flag
    assert H.pre_minutes(ser) == [606, 621, 930]


def test_control_is_later_and_uses_this_prereg_seed():
    sip = {k: (10.0, 10.0) for k in range(S.RTH_START, S.RTH_END)}
    c = H.control_minute("AAA", "2026-06-01", 600, sip)
    assert 601 <= c <= 660 and c == H.control_minute("AAA", "2026-06-01", 600, sip)
    picks = {H.control_minute("AAA", "2026-06-01", m, sip) - m for m in range(600, 700)}
    desk = {S.control_minute("AAA", "2026-06-01", m, sip) - m for m in range(600, 700)}
    assert picks != desk                                     # a different seed tag from the desk study
    assert H.control_minute("AAA", "2026-06-01", S.RTH_END - 5, sip) is None


def _rows(day, n=390, start=100.0, step=0.01):
    return np.array([[S.RTH_START + i, start + step * i, start + step * i + .02, start + step * i - .02,
                      start + step * i, 100.0] for i in range(n)])


def test_v_context_uses_prior_sessions_and_last_completed_close():
    days = [f"2026-06-{d:02d}" for d in range(1, 12)]
    prior = [(d, H.Session60(d, _rows(d))) for d in days[:10]]
    ctx = H.v_context("AAA", days[10], _rows(days[10], start=110.0), prior)
    assert ctx.prior_close == pytest.approx(100.0 + 0.01 * 389) and ctx.prior_low == pytest.approx(100.0 - .02)
    assert ctx.sessions5 == days[5:10]
    assert len(ctx.b60) == 7 * 11                            # 60-minute bars: 7 a session incl. the half bar
    t = BS.et_ts(days[10], 10, 31)                           # bar 10:30 ends at 10:31
    assert ctx.entry(t)[0] == pytest.approx(110.0 + 0.01 * 60)
    assert ctx.entry(t + 30)[0] == pytest.approx(110.0 + 0.01 * 60)   # mid-minute: still the last completed bar
    assert ctx.entry(BS.et_ts(days[10], 9, 30)) is None


def _pre(day, side, ctrl, sym="X"):
    return {"day": day, "sym": sym, "m": 600, "ctrl": ctrl, "sides": {c: side for c in H.CELLS}}


def test_count_h_floors_and_control_gap():
    rows = []
    for k in range(25):
        day = f"2026-06-{(k % 28) + 1:02d}" if k < 28 else None
        for i in range(13):
            rows.append(_pre(day, "IN", 0.001 + 0.0001 * (k % 3), sym=f"I{i}"))
            rows.append(_pre(day, "OUT", 0.0, sym=f"O{i}"))
    c = H.count_h(rows)["H1"]["A"]
    assert c["n_in"] == 325 and c["n_out"] == 325 and c["days_both_sides"] == 25 and c["scored"]
    assert c["ctrl_gap_in_minus_out"] > 0 and c["projected_mde"] > 0
    assert H.count_h(rows)["H1"]["B"]["scored"] is False
    thin = H.count_h(rows[:200])["H1"]["A"]
    assert thin["scored"] is False                           # < 300 IN entries


def test_count_v_and_funnels():
    rows = [{"day": f"2026-08-{(k % 20) + 1:02d}", "sym": f"S{k}", "m": 700, "setup": "S3", "ctrl": 0.0001 * (k % 5)}
            for k in range(320)]
    v = H.count_v(rows)
    assert v["S3"]["B"]["n"] == 320 and v["S3"]["B"]["days"] == 20 and v["S3"]["B"]["scored"]
    assert v["S4"]["B"]["n"] == 0 and not v["S4"]["B"]["scored"]
    fn = H.funnels(__import__("collections").Counter({"S3_entry": 4, "S3_skip_R": 2, "S4_entry": 1, "pre_onsets": 9}))
    assert fn["S3"] == {"entry": 4, "skip_R": 2} and fn["S4"] == {"entry": 1} and fn["S7"] == {}


def test_cross_name_is_another_eligible_name_and_seeded():
    sips = {"A": {601: 1, 610: 1}, "B": {601: 1, 610: 1}, "C": {601: 1}, "D": {}}
    pool = sorted(sips)
    x = H.cross_name("A", "2026-06-01", 600, pool, lambda n: sips[n])
    assert x == "B" and x == H.cross_name("A", "2026-06-01", 600, pool, lambda n: sips[n])
    assert H.cross_name("B", "2026-06-01", 600, ["B", "C", "D"], lambda n: sips[n]) is None
    big = {f"N{i}": {k: 1 for k in range(600, 720)} for i in range(200)}
    picks = {H.cross_name("N0", "2026-06-01", m, sorted(big), lambda n: big[n]) for m in range(600, 700)}
    assert "N0" not in picks and len(picks) > 50                # spread over the pool, never itself


def test_upper_bound():
    assert H.upper_bound(0.0001, 2.0, 30) == pytest.approx(0.0001 + S.t_ppf(0.975, 29) * 0.00005)
    assert H.upper_bound(0.0001, None, 30) is None and H.upper_bound(0.0, 0.0, 30) is None


def test_cell_verdict():
    lo, hi = (0.0004, 0.0003), (0.0006, 0.0004)
    assert H.cell_verdict(["PASS", "PASS"], [lo, lo]) == "PASS"
    assert H.cell_verdict(["FAIL", "PASS"], [lo, lo]) == "FAIL"
    assert H.cell_verdict(["NOT_PASSED", "NOT_PASSED"], [lo, lo]) == "FAIL"          # both bounds < 5 bp, both halves
    assert H.cell_verdict(["NOT_PASSED", "NOT_PASSED"], [lo, hi]) == "UNDERPOWERED"  # d's bound >= 5 bp in half B
    assert H.cell_verdict(["PASS", "NOT_PASSED"], [lo, lo]) == "FAIL"
    assert H.cell_verdict(["NOT_PASSED", "UNDERPOWERED"], [lo, (None, None)]) == "UNDERPOWERED"


def _gd(**kw):
    g = {"n_in": 400, "n_out": 400, "days": 30, "did": 0.0010, "did_t": 4.0, "t_crit": 2.7, "lift": 0.0005,
         "out_lift": -0.0005, "in_net": 0.0002, "did_nameday": 0.0008, "did_drop_names": 0.0004,
         "did_drop_days": 0.0003}
    g.update(kw)
    return g


def test_h_half_verdict_needs_both_controls():
    assert H.h_half_verdict({"d": _gd(), "x": _gd()}, "H1") == "PASS"
    assert H.h_half_verdict({"d": _gd(), "x": _gd(did=0.0004)}, "H1") == "NOT_PASSED"   # cross DiD < 5 bp
    assert H.h_half_verdict({"d": _gd(), "x": _gd(did_t=-2.1)}, "H1") == "FAIL"
    assert H.h_half_verdict({"d": _gd(n_in=299), "x": _gd()}, "H1") == "UNDERPOWERED"
    assert H.h_half_verdict({"d": _gd(days=19), "x": _gd()}, "H1") == "UNDERPOWERED"


def _vrow(day, sym, a, c, x, setup="S3"):
    return {"day": day, "sym": sym, "m": 700, "setup": setup, "o": {"arm": a, "ctrl": c, "cross": x}}


def test_v_grade_and_verdict():
    rng = __import__("random").Random(7)
    rows = [_vrow(f"2026-08-{(k % 25) + 1:02d}", f"N{k}", 0.0015 + rng.gauss(0, 0.001), 0.0, rng.gauss(0, 0.0005))
            for k in range(400)]
    g = H.v_grade(rows, "S3", "B")
    assert g["d"]["n"] == 400 and g["d"]["days"] == 25 and g["d"]["lift"] > 0.001 and g["x"]["lift"] > 0.001
    assert H.v_half_verdict(g) == "PASS"
    bad = [_vrow(r["day"], r["sym"], r["o"]["arm"], 0.0, r["o"]["arm"] + rng.gauss(0, 0.0005))
           for r in rows]                                    # no lift over the cross control
    assert H.v_half_verdict(H.v_grade(bad, "S3", "B")) == "NOT_PASSED"
    assert H.v_half_verdict(H.v_grade(rows[:250], "S3", "B")) == "UNDERPOWERED"


def test_coin_is_seeded_and_fair():
    rows = [{"sym": f"N{i}", "day": "2026-06-01", "m": 600 + i % 300} for i in range(4000)]
    flips = [H.coin(r) for r in rows]
    assert flips == [H.coin(r) for r in rows] and 0.45 < flips.count("IN") / len(flips) < 0.55


def test_bound_check():
    ok = H.bound_check([{"t": 1.5}, {"t": -2.0}, {"t": None}])
    assert ok["n_stats"] == 2 and ok["ok"] and ok["bound"] == pytest.approx(S.t_ppf(1 - 0.025 / 2, 10_000))
    assert not H.bound_check([{"t": 3.5}, {"t": 0.1}])["ok"] and not H.bound_check([{"t": None}])["ok"]


def test_score_gate_refusals(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(H, "OUT_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="skeptic-ready"):
        H.score_gate("", [])
    with pytest.raises(SystemExit, match="count.json missing"):
        H.score_gate("READY", [])
    sha = H.script_sha()
    (tmp_path / "count.json").write_text(json.dumps({"script_sha256": sha, "scored_sessions": ["d1"]}))
    (tmp_path / "null.json").write_text(json.dumps({"script_sha256": sha, "ok": True}))
    (tmp_path / "placebo.json").write_text(json.dumps({"script_sha256": sha, "ok": False}))
    with pytest.raises(SystemExit, match="placebo.json is NOT OK"):
        H.score_gate("READY", ["d1"])
    (tmp_path / "placebo.json").write_text(json.dumps({"script_sha256": "old", "ok": True}))
    with pytest.raises(SystemExit, match="different script version"):
        H.score_gate("READY", ["d1"])
    (tmp_path / "placebo.json").write_text(json.dumps({"script_sha256": sha, "ok": True}))
    with pytest.raises(SystemExit, match="handcheck.md missing"):
        H.score_gate("READY", ["d1"])
    (tmp_path / "handcheck.md").write_text("x")
    with pytest.raises(SystemExit, match="session set changed"):
        H.score_gate("READY", ["d1", "d2"])
    assert H.score_gate("READY", ["d1"])["scored_sessions"] == ["d1"]


def test_pipeline_end_to_end_on_the_synthetic_walk(monkeypatch):
    """null and score modes run every code path once on a tiny walk (12 sessions = 10 look-back + 2, 8 names)."""
    monkeypatch.setattr(H, "NULL_NAMES", 8)
    monkeypatch.setitem(H.SOURCES, "null", lambda feed, day: H.null_raw(feed, day, n=8))
    days = H.null_days(LOOKBACK_PLUS := H.LOOKBACK + 2)
    ks = list(range(H.LOOKBACK, LOOKBACK_PLUS))
    res = H.run_days(days, ks, "null", "score", workers=1, quiet=True)
    assert res["pre"] and all("o" in r and "f" in r and "v_recent" in r for r in res["pre"])
    assert all(r["c"] > r["m"] for r in res["pre"])
    half_end = days[H.LOOKBACK]
    count = {"h": {c: {h: {"scored": True} for h in "AB"} for c in H.CELLS},
             "v": {s: {h: {"scored": True} for h in "AB"} for s in H.V_SETUPS}}
    monkeypatch.setattr(H, "HALF_A_END", half_end)
    out = H.score(res, count)
    assert set(out["verdicts"]) == set(H.CELLS) | set(H.V_SETUPS)
    assert all(v == "UNDERPOWERED" for v in out["verdicts"].values())       # 2 sessions: below every floor
    md = H.report(out, {"sessions": days[H.LOOKBACK:], "counts": dict(res["counts"])})
    assert "## V setups" in md and "base population" in md and "top 1% of name-days" in md
    checks = H.all_t(res["pre"], res["v"], half_end)
    assert {c["stat"] for c in checks} >= {"DiD_d", "DiD_x"}
