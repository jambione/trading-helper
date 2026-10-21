"""Indicators, population, control, statistics and gating for indicator_confirm_study. No market data."""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import early_entry_honest as E  # noqa: E402
import indicator_confirm_study as S  # noqa: E402


def _ts(day: str, minute: int) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=minute // 60, minute=minute % 60,
                                                      tzinfo=E.ET).timestamp()


def _bars(closes, start=S.RTH_START, vol=100.0, spread=0.01):
    return {start + i: (c, c + spread, c - spread, c, vol) for i, c in enumerate(closes)}


def test_t_ppf_matches_published_quantiles():
    assert S.t_ppf(0.995, 12) == pytest.approx(3.0545, abs=1e-3)
    assert S.t_ppf(0.995, 17) == pytest.approx(2.8982, abs=1e-3)
    assert S.t_ppf(0.975, 10_000) == pytest.approx(1.960, abs=2e-3)
    assert S.t_crit(13) == pytest.approx(3.0545, abs=1e-3)      # df = days - 1
    assert S.t_ppf(0.995, 1) == pytest.approx(63.657, abs=1e-2)   # df 1 needs a wide bracket
    assert S.t_crit(1) is None


def test_adx_valid_after_28_bars_and_reads_direction():
    up = [10 + 0.05 * i for i in range(40)]
    adx, pdi, mdi = S.wilder_adx([c + 0.01 for c in up], [c - 0.01 for c in up], up)
    assert all(a is None for a in adx[:27]) and adx[27] is not None
    assert pdi[30] > mdi[30] and adx[39] > 25
    chop = [10 + (0.05 if i % 2 else 0) for i in range(40)]
    a2, _, _ = S.wilder_adx([c + 0.01 for c in chop], [c - 0.01 for c in chop], chop)
    assert a2[39] < 20


def test_ema_seeded_by_sma_and_rsi_seed():
    e = S.ema([1.0, 2.0, 3.0, 4.0], 3)
    assert e[:2] == [None, None] and e[2] == pytest.approx(2.0) and e[3] == pytest.approx(0.5 * 4 + 0.5 * 2)
    r = S.rsi_wilder([float(i) for i in range(20)], 14)
    assert r[13] is None and r[14] == 100.0
    r2 = S.rsi_wilder([10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10], 14)
    assert r2[14] == pytest.approx(50.0)


def test_vwap_obv_ad():
    assert S.vwap([3.0, 5.0], [1.0, 3.0], [2.0, 4.0], [1.0, 3.0])[1] == pytest.approx((2 * 1 + 4 * 3) / 4)
    assert S.obv([1.0, 2.0, 2.0, 1.0], [5.0, 7.0, 9.0, 4.0]) == [0.0, 7.0, 7.0, 3.0]
    assert S.ad_line([2.0, 2.0], [0.0, 1.0], [2.0, 1.0], [10.0, 10.0]) == [10.0, 0.0]   # close at high +v, at low -v
    assert S.ad_line([1.0], [1.0], [1.0], [10.0]) == [0.0]                             # high = low adds 0


def test_rth_bars_drop_premarket():
    import pandas as pd
    t = [pd.Timestamp(_ts("2026-10-01", m), unit="s", tz="UTC") for m in (9 * 60, 9 * 60 + 30, 16 * 60)]
    f = pd.DataFrame({"time": t, "open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3, "close": [1.0] * 3,
                      "volume": [1.0] * 3})
    assert list(S.rth_bars(f)) == [9 * 60 + 30]


def test_confirmation_states_use_bars_before_m():
    closes = [10.0] * 30 + [10.1]
    bars = _bars(closes)
    ind = S.indicators(bars)
    m = S.RTH_START + 31                       # bar m-1 is the 10.1 close: a 30-minute high
    f = S.features_at(ind, m)
    assert f["obv"] == "CONFIRMS"
    vol = {10: 1000.0, 11: 300.0}               # spike up, heavy dip, then a light new high
    closes2 = [10.0] * 10 + [10.2] + [10.15] * 19 + [10.21]
    b2 = {S.RTH_START + i: (c, c + .01, c - .01, c, vol.get(i, 100.0)) for i, c in enumerate(closes2)}
    f2 = S.features_at(S.indicators(b2), m)
    assert f2["obv"] == "DIVERGES"            # price at a new high, OBV below its spike-bar max
    f3 = S.features_at(S.indicators(_bars([10.0] * 30 + [9.9])), m)
    assert f3["obv"] == "NOT_AT_HIGH"
    # a future bar never changes the feature
    b4 = dict(bars)
    b4[m] = (50.0, 50.0, 50.0, 50.0, 1e9)
    assert S.features_at(S.indicators(b4), m) == f


def test_thin_window_is_none():
    bars = {k: v for k, v in _bars([10.0] * 60).items() if k % 3 == 0}   # a third of the minutes
    assert S.features_at(S.indicators(bars), S.RTH_START + 60) == {"ok": False}


def test_c1_needs_1031_and_all_four_legs():
    closes = [10 + 0.01 * i for i in range(65)]
    ind = S.indicators(_bars(closes))
    assert S.c1_at(ind, S.C1_START - 1, 0) == (None, None)
    m = S.RTH_START + 65
    ok, legs = S.c1_at(ind, m, len(closes) - 1)
    assert legs["trend"] is True and ok == all(legs.values())
    assert legs["rsi_turn"] is False           # a steady climb keeps RSI near 100, outside 38-52


def test_arms_true_only_and_refusals_never_block():
    day = "2026-10-01"
    rows = [
        {"stage": "arm", "symbol": "aaa", "ts": _ts(day, 10 * 60), "arm_ok": None, "arm_why": "tape_only"},
        {"stage": "arm", "symbol": "AAA", "ts": _ts(day, 10 * 60 + 5), "arm_ok": True},
        {"stage": "arm", "symbol": "AAA", "ts": _ts(day, 10 * 60 + 10), "arm_ok": True},    # inside 15 min
        {"stage": "arm", "symbol": "AAA", "ts": _ts(day, 10 * 60 + 20), "arm_ok": True},
        {"stage": "watch", "symbol": "BBB", "ts": _ts(day, 11 * 60), "arm_ok": True},
        {"stage": "arm", "symbol": "CCC", "ts": _ts(day, 9 * 60 + 59), "arm_ok": True},      # before 10:00
        {"stage": "arm", "symbol": "CCC", "ts": _ts(day, 15 * 60 + 31), "arm_ok": True},     # after 15:30
    ]
    assert S.arms_from_rows(rows) == [("AAA", 605), ("AAA", 620)]
    assert S.arms_from_rows(rows, ok_only=False) == [("AAA", 600), ("AAA", 620)]


def test_control_is_later_seeded_and_in_rth():
    sip = {k: (10.0, 10.0) for k in range(S.RTH_START, S.RTH_END)}
    c = S.control_minute("AAA", "2026-10-01", 600, sip)
    assert 601 <= c <= 660 and c == S.control_minute("AAA", "2026-10-01", 600, sip)
    assert S.control_minute("AAA", "2026-10-01", 600, sip, lo=11) >= 611
    assert S.control_minute("AAA", "2026-10-01", S.RTH_END - 5, sip) is None
    assert S.net({600: (10.0, 10.0), 601: (10.0, 10.0), 610: (10.0, 10.1)}, 600) == pytest.approx(0.01 - S.COST)


def _rec(day, sym, side, d, a=0.001):
    return {"day": day, "sym": sym, "side": side, "o": {"arm": a, "ctrl": a - d}}


def test_did_is_in_minus_out_of_paired_differences():
    recs = []
    for k in range(10):
        day = f"2026-09-{10 + k:02d}"
        for i in range(3):
            recs.append(_rec(day, f"I{i}", "IN", 0.0010 + 0.0001 * (k % 3)))
            recs.append(_rec(day, f"O{i}", "OUT", 0.0002))
    g = S.grade_half(recs, lambda r: r["side"])
    assert g["n_in"] == 30 and g["n_out"] == 30 and g["days"] == 10
    assert g["did"] == pytest.approx(0.0001 * (10 + 1) - 0.0002 + 0.00001 * 0, rel=0.2)
    assert g["did"] > 0 and g["did_t"] > 0 and g["lift"] > g["out_lift"]
    assert len(g["dropped_names"]) == 3 and len(g["dropped_days"]) == 3


def test_days_with_too_few_arms_a_side_do_not_count():
    recs = [_rec("2026-09-10", "I", "IN", 0.001), _rec("2026-09-10", "O", "OUT", 0.0)]
    assert S.grade_half(recs, lambda r: r["side"])["days"] == 0


def _g(**kw):
    g = {"n_in": 200, "n_out": 200, "days": 10, "did": 0.0010, "did_t": 4.0, "t_crit": 3.05, "lift": 0.0005,
         "out_lift": -0.0005, "in_net": 0.0002, "did_nameday": 0.0008, "did_drop_names": 0.0004,
         "did_drop_days": 0.0003}
    g.update(kw)
    return g


def test_half_verdict_rules():
    assert S.half_verdict(_g(), "H1") == "PASS"
    assert S.half_verdict(_g(n_in=99), "H1") == "UNDERPOWERED"
    assert S.half_verdict(_g(did_t=-2.0), "H1") == "FAIL"
    assert S.half_verdict(_g(in_net=-0.0001), "H1") == "NOT_PASSED"
    assert S.half_verdict(_g(out_lift=0.0001), "H2") == "NOT_PASSED"           # H2 needs OUT lift < 0
    assert S.half_verdict(_g(out_lift=0.0001), "H1") == "PASS"
    assert S.half_verdict(_g(did_t=3.0), "H1") == "NOT_PASSED"                 # below t_crit 3.05


def test_final_verdict():
    assert S.final_verdict(["PASS", "PASS"], [0.001, 0.001]) == "PASS"
    assert S.final_verdict(["FAIL", "PASS"], [0.004, 0.001]) == "FAIL"           # t <= -2 in a scored half
    assert S.final_verdict(["NOT_PASSED", "NOT_PASSED"], [0.001, 0.0015]) == "FAIL"
    assert S.final_verdict(["PASS", "NOT_PASSED"], [0.001, 0.001]) == "FAIL"     # both powered, pass not met
    # skeptic 10/21 #1: one half's own MDE never fails the cell (H2: A 45 bp, B 13 bp)
    assert S.final_verdict(["PASS", "NOT_PASSED"], [0.0045, 0.0013]) == "UNDERPOWERED"
    assert S.final_verdict(["NOT_PASSED", "NOT_PASSED"], [0.0045, 0.0013]) == "UNDERPOWERED"
    assert S.final_verdict(["PASS", "UNDERPOWERED"], [0.001, None]) == "UNDERPOWERED"
    assert S.final_verdict(["FAIL", "UNDERPOWERED"], [0.001, None]) == "FAIL"


def test_unscored_cell_half_prints_counts_only():
    recs = []
    for k in range(4):
        for i in range(3):
            recs.append({**_rec(f"2026-09-{10 + k:02d}", f"I{i}", "IN", 0.001), "half": "A"})
            recs.append({**_rec(f"2026-09-{10 + k:02d}", f"O{i}", "OUT", 0.0), "half": "A"})
    lines = S.table(recs, {"H1": lambda r: r["side"]}, scored={"H1": {"A": False, "B": True}})
    assert "not scored" in lines[2] and "+10.0" not in lines[2]
    assert "+10.0" in S.table(recs, {"H1": lambda r: r["side"]})[2]


def test_cell_sides():
    f = {"ok": True, "adx": 26.0, "pdi": 30.0, "mdi": 10.0, "obv": "CONFIRMS", "ad": "NOT_AT_HIGH", "c1": True}
    assert S.cell_side(f, "H1", 700) == "IN" and S.cell_side(f, "H2", 700) == "IN"
    assert S.cell_side({**f, "pdi": 5.0}, "H1", 700) == "OUT"
    assert S.cell_side({**f, "adx": 19.0}, "H2", 700) == "OUT"
    assert S.cell_side(f, "H3", 700) == "IN" and S.cell_side(f, "H4", 700) is None
    assert S.cell_side(f, "H5", S.C1_START - 1) is None and S.cell_side(f, "H5", S.C1_START) == "IN"
    assert S.cell_side({"ok": False}, "H1", 700) is None


def test_null_arms_are_30_minute_highs():
    import pandas as pd
    day = "2026-06-01"
    closes = [10.0 + (0.01 * i if i > 40 else 0) for i in range(390)]
    t = [pd.Timestamp(_ts(day, S.RTH_START + i), unit="s", tz="UTC") for i in range(390)]
    fr = pd.DataFrame({"time": t, "open": closes, "high": closes, "low": closes, "close": closes,
                       "volume": [1.0] * 390})
    arms = S.null_arms({"X": fr})
    assert arms[0] == ("X", S.ARM_START) and all(b[1] - a[1] >= S.REFRACT for a, b in zip(arms, arms[1:]))


def test_score_refuses_without_skeptic_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(S, "load_study_days", lambda ok_only=True: ([], {}, [], Counter()))
    monkeypatch.setattr(sys, "argv", ["x", "--score"])
    with pytest.raises(SystemExit, match="skeptic-ready"):
        S.main()
    monkeypatch.setattr(sys, "argv", ["x", "--score", "--skeptic-ready", "READY"])
    with pytest.raises(SystemExit, match="count step"):
        S.main()
    import json
    (tmp_path / "count.json").write_text(json.dumps({"script_sha256": "old", "days": []}))
    (tmp_path / "null.json").write_text(json.dumps({"ok": True, "script_sha256": S.script_sha()}))
    with pytest.raises(SystemExit, match="different script version"):
        S.main()
