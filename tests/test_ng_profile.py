"""ng_profile: AUC direction, a label-only feature is flagged, a feature that also moves net is not, outcome-stamped fields are excluded."""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import ng_profile as npf  # noqa: E402


def _rows(n=900, seed=3):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        quiet = rng.random()                              # low = quiet tape
        ng = rng.random() < (0.65 - 0.4 * quiet)          # quiet -> more never-green
        bp = rng.gauss(-7, 8)                             # but net does NOT depend on quiet (label artifact)
        edge = rng.random()
        ng2 = rng.random() < 0.5
        bp2 = rng.gauss(-14 + 14 * edge, 6)               # `edge` moves net strongly, never-green not at all
        d = f"2026-10-{1 + i * 28 // n:02d}"
        rows.append({"d": d, "ng": ng if i % 2 == 0 else ng2, "bp": bp if i % 2 == 0 else bp2,
                     "f": {"quiet_feat": quiet if i % 2 == 0 else rng.random(), "edge_feat": edge if i % 2 == 1 else rng.random(),
                           "extension_class": 1.0, "mfe_r": 0.5}, "src": "movers"})
    return rows


def test_auc_direction_and_ties():
    assert npf.auc([1, 2, 3], [4, 5, 6]) == 0.0
    assert npf.auc([4, 5, 6], [1, 2, 3]) == 1.0
    assert abs(npf.auc([1, 1], [1, 1]) - 0.5) < 1e-9


def test_label_only_feature_is_flagged_and_outcome_fields_excluded():
    res = {x["k"]: x for x in npf.profile(_rows(), min_n=100)}
    assert "extension_class" not in res
    q = res["quiet_feat"]
    assert q["auc"] < 0.45 and q["ng_spread"] >= 0.15 and q["label_only"] is True


def test_brief_renders(tmp_path, monkeypatch):
    rows = _rows()
    monkeypatch.setattr(npf, "load", lambda *a, **k: rows)
    text = "\n".join(npf.brief())
    assert "Never-green profile" in text and "quiet_feat" in text and "LABEL ONLY" in text
