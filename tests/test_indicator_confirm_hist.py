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
