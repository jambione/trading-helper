"""Rules, timing, controls, outcomes, gates and an end-to-end synthetic run for three_minute_reread."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import three_minute_reread as T  # noqa: E402

H, S, E = T.H, T.S, T.E


def test_null_seed_tag_is_this_studys():
    assert H.NULL_SEED_TAG == "three-min-null"


def test_confirm_env_points_the_loader_at_the_held_out_directory():
    code = ("import sys; sys.path.insert(0, 'tools/studies'); import three_minute_reread as T; "
            "print(T.H.WH == T.CONFIRM_DIR)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "TMR_SAMPLE": "confirm"}, cwd=os.path.join(os.path.dirname(__file__), ".."))
    assert out.stdout.strip() == "True"
    assert H.WH != T.CONFIRM_DIR                       # not set without the env


def test_rule_minutes_are_onset_plus_one_in_window(monkeypatch):
    mins = list(range(9 * 60 + 50, 15 * 60 + 35))
    ser = {"minutes": mins, "fast": [0.0] * len(mins), "slow": [0.0] * len(mins), "rsi": [50.0] * len(mins),
           "close": [10.0] * len(mins), "vol": [1.0] * len(mins), "con_minutes": mins,
           "con_flags": [m in (10 * 60 + 2, 15 * 60 + 27) for m in mins]}
    fire = {9 * 60 + 59, 10 * 60 + 10, 10 * 60 + 20, 15 * 60 + 26, 15 * 60 + 27}
    for name in ("pre_flag", "sq_flag", "bothup_flag", "os_leave_flag"):
        monkeypatch.setattr(E, name, lambda f, s, i: mins[i] in fire)
    monkeypatch.setattr(E, "mid_flag", lambda f, s, r, i: False)
    monkeypatch.setattr(E, "rsi2_turn_flag", lambda f, r, i: False)
    monkeypatch.setattr(E, "ign_flag", lambda m, c, v, i, va: False)
    out = T.rule_minutes(ser)
    # 09:59 -> m 10:00; 10:10 is 11 min after the kept 09:59 (refractory) -> skipped; 10:20 -> m 10:21;
    # 15:26 -> m 15:27; 15:27 only continues the 15:26 flag
    assert out["PRE"] == [600, 621, 927] and out["SQ"] == out["PRE"]
    assert out["MID"] == [] and out["IGN"] == []
    assert out["CON"] == [603]                         # the 15:27 onset is after the 15:26 window


def test_control_and_net_use_the_lag_zero_three_bar_window():
    sip = {k: (10.0, 10.0) for k in range(S.RTH_START, S.RTH_END)}
    c = T.control_minute("AAA", "2026-06-01", 600, sip)
    assert 601 <= c <= 660 and c == T.control_minute("AAA", "2026-06-01", 600, sip)
    assert T.control_minute("AAA", "2026-06-01", S.RTH_END - 3, sip) is None
    sip2 = {600: (10.0, 10.1), 601: (10.1, 10.2), 602: (10.2, 10.3)}
    assert T.net(sip2, 600) == pytest.approx(10.3 / 10.0 - 1 - T.COST)        # open of m to close of m+2
    assert T.net(sip2, 600, lag=1) is None                                      # lag 1 needs bar 603
    assert T.excursion({600: (10.0, 10.2, 9.9, 10.1, 1), 601: (10.1, 10.4, 10.0, 10.3, 1),
                        602: (10.3, 10.3, 9.8, 10.0, 1)}, 600) == (pytest.approx(0.04), pytest.approx(-0.02))


def test_cross_name_needs_bars_at_m_and_m_plus_2():
    sips = {"A": {600: 1, 602: 1}, "B": {600: 1, 602: 1}, "C": {600: 1, 601: 1}}
    assert T.cross_name("A", "2026-06-01", 600, sorted(sips), lambda n: sips[n]) == "B"
    assert T.cross_name("B", "2026-06-01", 600, ["B", "C"], lambda n: sips[n]) is None


def test_t_crit18():
    assert T._t_crit18(55) == pytest.approx(S.t_ppf(1 - 0.025 / 18, 54))
    assert 3.0 < T._t_crit18(55) < 3.3 and T._t_crit18(1) is None


def test_count_labels_floors():
    rows = [{"day": f"2026-08-{(k % 22) + 1:02d}", "sym": f"N{k}", "m": 700, "setup": "SQ", "ctrl": 0.0001 * (k % 7)}
            for k in range(330)]
    c = T.count_labels(rows, ("SQ", "MID"))
    assert c["SQ"]["B"]["n"] == 330 and c["SQ"]["B"]["days"] == 22 and c["SQ"]["B"]["scored"]
    assert not c["MID"]["B"]["scored"] and not c["SQ"]["A"]["scored"]


def _g(**kw):
    g = {"n": 400, "days": 30, "lift": 0.0003, "t": 3.5, "net": 0.0001, "lift_nameday": 0.0003, "drop_names": 0.0001,
         "drop_days": 0.0001}
    g.update(kw)
    return g


def test_confirm_half():
    assert T.confirm_half({"d": _g(), "x": _g()}, False, 1) == "PASS"
    assert T.confirm_half({"d": _g(), "x": _g(t=1.5)}, False, 1) == "NOT_PASSED"         # x below t at p 0.025
    assert T.confirm_half({"d": _g(t=2.2), "x": _g(t=2.2)}, False, 3) == "NOT_PASSED"    # Bonferroni over k = 3
    assert T.confirm_half({"d": _g(n=250), "x": _g()}, False, 1) == "UNDERPOWERED"
    assert T.confirm_half({"d": _g(net=-0.0001), "x": _g()}, False, 1) == "NOT_PASSED"


def test_session_asserts():
    T.assert_discovery(["2026-05-11", "2026-07-01", "2026-10-15"])
    with pytest.raises(SystemExit):
        T.assert_discovery(["2026-05-11", "2026-10-16"])
    T.assert_confirm(["2026-01-02", "2026-04-24"])
    with pytest.raises(SystemExit):
        T.assert_confirm(["2026-01-02", "2026-04-27"])


def test_score_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "OUT_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="skeptic-ready"):
        T.score_gate("", [])
    ms = T.module_shas()
    assert "three_minute_reread" in ms and "indicator_confirm_hist" in ms
    good_null = {"module_sha256": ms, "ok": True, "days": 120, "seed_tag": "three-min-null"}
    (tmp_path / "count.json").write_text(json.dumps({"module_sha256": ms, "scored_sessions": ["d1"]}))
    (tmp_path / "placebo.json").write_text(json.dumps({"module_sha256": ms, "ok": True}))
    (tmp_path / "null.json").write_text(json.dumps({**good_null, "seed_tag": "ich-null-confirm"}))
    with pytest.raises(SystemExit, match="three-min-null"):
        T.score_gate("READY", ["d1"])
    (tmp_path / "null.json").write_text(json.dumps({**good_null, "investigate": ["H2 A DiD_d t=-2.10"]}))
    with pytest.raises(SystemExit, match="investigate"):
        T.score_gate("READY", ["d1"])
    (tmp_path / "null.json").write_text(json.dumps({**good_null, "module_sha256": {**ms, "signals": "x"}}))
    with pytest.raises(SystemExit, match="different script or module"):
        T.score_gate("READY", ["d1"])
    (tmp_path / "null.json").write_text(json.dumps(good_null))
    assert T.score_gate("READY", ["d1"])["scored_sessions"] == ["d1"]


def test_end_to_end_on_the_synthetic_walk(monkeypatch):
    monkeypatch.setitem(H.SOURCES, "null", lambda feed, day: H.null_raw(feed, day, n=8))
    H._RAW.clear()
    days = H.null_days(H.LOOKBACK + 2)
    ks = list(range(H.LOOKBACK, len(days)))
    res = T.run_days(days, ks, "null", "score", workers=1, quiet=True)
    labels = {r["setup"] for r in res["rows"]}
    assert "PRE" in labels and all("o" in r for r in res["rows"])
    assert all(r["c"] > r["m"] for r in res["rows"])
    assert all("sides" in r for r in res["rows"] if r["setup"] == "PRE")
    monkeypatch.setattr(H, "HALF_A_END", days[H.LOOKBACK])
    count = {"standalone": {lab: {h: {"scored": True} for h in "AB"} for lab in T.STANDALONE},
             "filters": {c: {h: {"scored": True} for h in "AB"} for c in T.FILTERS}}
    out = T.verdicts(res["rows"], count)
    assert set(out["verdicts"]) == set(T.STANDALONE) | set(T.FILTERS)
    assert all(v == "UNDERPOWERED" for v in out["verdicts"].values())
    out["info"] = T.info_tables(res["rows"], count)
    md = T.report(out, days[H.LOOKBACK:], "synthetic")
    assert "| PRE |" in md and "standalone entries" in md
    stats = T.checks(res["rows"], days[H.LOOKBACK])
    assert {c["cell"] for c in stats} == set(T.STANDALONE) | set(T.FILTERS)
    H._RAW.clear()
