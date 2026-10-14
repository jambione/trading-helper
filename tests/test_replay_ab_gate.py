"""Pure parts of the replay A/B gate. No replays run."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import replay_ab_gate as G  # noqa: E402


def _t(sym, ts, ret):
    return {"symbol": sym, "entry_ts": ts, "exit_ts": ts + 60, "ret": ret, "reason": "local_trail"}


def _res(rets, slots=39):
    return {"closed": [_t("X", 1000 + 600 * i, r) for i, r in enumerate(rets)], "open_at_end": [],
            "slots": {str(k): {} for k in range(slots)}}


def test_parse_sets_json_or_string():
    assert G.parse_sets(["a=1", "b=true", "c=09:45", 'd="x"']) == {"a": 1, "b": True, "c": "09:45", "d": "x"}


def test_compare_day_pairs_and_charges():
    d = G.compare_day(_res([0.001] * 10), _res([0.002] * 10))
    assert d["n_paired"] == 10 and abs(d["paired"] - 0.001) < 1e-12
    assert abs(d["charged_cand"] - d["charged_base"] - 0.01) < 1e-12


def test_gate_flags_thin_book_and_worse_totals():
    same = {f"d{i}": G.compare_day(_res([0.001] * 12), _res([0.001] * 12)) for i in range(4)}
    assert G.gate(same) == ("NO-REGRESSION", [])
    thin = {f"d{i}": G.compare_day(_res([0.001] * 12), _res([0.001] * 9)) for i in range(4)}
    v, why = G.gate(thin)
    assert v == "REGRESSION" and any("opens" in w for w in why)
    worse = {f"d{i}": G.compare_day(_res([0.001] * 12), _res([-0.001] * 12)) for i in range(4)}
    v, why = G.gate(worse)
    assert v == "REGRESSION" and any("charged" in w for w in why) and any("per-trade" in w for w in why)
    small = {f"d{i}": G.compare_day(_res([0.001] * 5), _res([0.001] * 5)) for i in range(4)}
    assert G.gate(small)[0] == "UNDERPOWERED"


def test_recorded_days_needs_done_marker(tmp_path):
    for d, done in (("2026-10-12", True), ("2026-10-13", True), ("2026-10-14", False)):
        (tmp_path / d).mkdir()
        if done:
            (tmp_path / d / "DONE").write_text("")
    assert G.recorded_days(5, tmp_path) == ["2026-10-12", "2026-10-13"]
    assert G.recorded_days(1, tmp_path) == ["2026-10-13"]
