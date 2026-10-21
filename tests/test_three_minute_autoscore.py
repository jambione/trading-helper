"""The autoscore gate scores only when every round-2 READY condition holds."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import three_minute_autoscore as A  # noqa: E402

T = A.T


def _good(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(T, "check_confirm_sample",
                        lambda: ["2026-01-14"] + [f"x{i}" for i in range(68)] + ["2026-04-24"])
    ms = T.module_shas()
    cells = {lab: {h: {"scored": True, "n": 500} for h in "AB"} for lab in T.STANDALONE}
    filt = {c: {h: {"scored": True} for h in "AB"} for c in T.FILTERS}
    sess = ["2026-05-11"] + [f"s{i}" for i in range(108)] + ["2026-10-15"]
    (tmp_path / "count.json").write_text(json.dumps({"module_sha256": ms, "scored_sessions": sess,
                                                     "counts": {"RSI2T_onsets": 457_699},
                                                     "standalone": cells, "filters": filt}))
    (tmp_path / "null.json").write_text(json.dumps({"module_sha256": ms, "ok": True, "days": 120,
                                                    "seed_tag": "three-min-null", "n_stats": 72, "max_abs_t": 2.37}))
    (tmp_path / "placebo.json").write_text(json.dumps({"module_sha256": ms, "ok": True, "n_stats": 20}))


def test_all_conditions_met(tmp_path, monkeypatch):
    _good(tmp_path, monkeypatch)
    assert A.ready_to_check() and A.conditions() == []


def test_each_condition_blocks(tmp_path, monkeypatch):
    _good(tmp_path, monkeypatch)
    n = json.loads((tmp_path / "null.json").read_text())
    (tmp_path / "null.json").write_text(json.dumps({**n, "max_abs_t": 2.60}))
    assert any("something else changed" in b for b in A.conditions())
    (tmp_path / "null.json").write_text(json.dumps({**n, "investigate": ["H2 A DiD_d t=-2.10"]}))
    assert any("investigate" in b for b in A.conditions())
    (tmp_path / "null.json").write_text(json.dumps(n))
    p = json.loads((tmp_path / "placebo.json").read_text())
    (tmp_path / "placebo.json").write_text(json.dumps({**p, "ok": False}))
    assert any(b.startswith("placebo") for b in A.conditions())
    (tmp_path / "placebo.json").write_text(json.dumps({**p, "module_sha256": {"x": "y"}}))
    assert not A.ready_to_check()                                   # written by other code: keep waiting
    (tmp_path / "placebo.json").write_text(json.dumps(p))
    c = json.loads((tmp_path / "count.json").read_text())
    c["standalone"]["S4"]["B"]["scored"] = False
    (tmp_path / "count.json").write_text(json.dumps(c))
    assert "count: S4 B not scored" in A.conditions()
    monkeypatch.setattr(T, "check_confirm_sample", lambda: ["2026-01-13"] + ["x"] * 68 + ["2026-04-24"])
    assert any(b.startswith("fetch range") for b in A.conditions())
