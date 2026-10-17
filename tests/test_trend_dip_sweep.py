"""trend_dip_sweep: causal selection at 10:30, earliest dip-and-turn onset, end-to-end on a synthetic warehouse."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
sys.path.insert(0, os.path.dirname(__file__))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402
import trend_dip_sweep as D  # noqa: E402
from test_prescreen_turn import _day  # noqa: E402


def test_dip_onset_needs_dip_turn_and_up_on_day():
    m = list(range(640, 660))
    fast = np.array([-60, -70, -85, -92, -88, -80, -70, -60] + [-50] * 12, float)
    close = np.array([10.5] * 20)
    assert D.dip_onsets(m, fast, close, 10.0, -90.0) == [644]            # first rising bar after the -92 dip
    assert D.dip_onsets(m, fast, close, 11.0, -90.0) == []               # below the prior close: not trending
    assert D.dip_onsets(m, fast, close, 10.0, -95.0) == []               # never dipped that far
    early = list(range(600, 620))                                        # before 10:31: not watched
    assert D.dip_onsets(early, fast, close, 10.0, -90.0) == []


def test_selection_uses_only_bars_before_1030(tmp_path, monkeypatch):
    for f in ("iex", "sip"):
        os.makedirs(tmp_path / f)
    for k, day in enumerate(("2026-10-05", "2026-10-06")):
        for f in ("iex", "sip"):
            _day(tmp_path / f / f"{day}.npz", day, k)
    monkeypatch.setattr(P, "WH", str(tmp_path))
    monkeypatch.setattr(D, "TREND_MIN", -1.0)                            # synthetic walks: let any name qualify
    sip_t = P._frames(str(tmp_path / "sip" / "2026-10-06.npz"))
    sip_p = P._frames(str(tmp_path / "sip" / "2026-10-05.npz"))
    a = D.select_trending(sip_t, sip_p)
    for s in sip_t:                                                      # changing bars after 10:29 changes nothing
        sip_t[s].loc[sip_t[s]["time"].map(lambda t: E._et_min(t.timestamp())) >= D.PICK_MIN, "close"] *= 3
    assert D.select_trending(sip_t, sip_p) == a
    monkeypatch.setattr(E, "WARM_LINES", True)
    monkeypatch.setattr(D, "macro_class", lambda sym, prev: "unknown")
    day, acc = D.day_job(("2026-10-06", "2026-10-05"))
    info = acc.pop("_info")
    assert set(acc) == set(D.CELLS) and info["n_top"] == len(a) and info["ind_fail"] == 0
    md = D.report(D.summarize({day: {**acc, "_info": info}}, [day], day), {day: {**acc, "_info": info}}, [day], day)
    assert "Trending names" in md


def test_macro_class(monkeypatch):
    up = {f"2026-{1 + k // 28:02d}-{1 + k % 28:02d}": (1.0, 10.0 + 0.1 * k) for k in range(60)}
    monkeypatch.setattr(D, "_DAILY", {"UP": up, "DN": {k: (1.0, 20.0 - 0.1 * i) for i, k in enumerate(sorted(up))}})
    last = sorted(up)[-1]
    assert D.macro_class("UP", last) == "up" and D.macro_class("DN", last) == "other"
    assert D.macro_class("UP", sorted(up)[40]) == "unknown" and D.macro_class("ZZZ", last) == "unknown"
