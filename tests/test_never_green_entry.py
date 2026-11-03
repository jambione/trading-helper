"""never_green_entry.py (docs/studies/never_green_entry_prereg.json): pure parts."""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import never_green_entry as N  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(hm, day="2026-10-14"):
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()


def _o(**kw):
    o = {"symbol": "ABCD", "entry_path": "watch", "entry_price": 50.0, "exit_price": 50.1,
         "entry_time": _t("10:30") + 7, "exit_time": _t("10:35"), "peak_price": 50.2, "arm_why": "last_presquare",
         "pctr": -30.0, "pctr_slow": -40.0, "entry_exhaustion": 60.0,
         "extension_class": "extended", "mfe_r": 3.0,
         "features": {"source": "movers", "cm_rsi": 55.0, "pct_change": 4.0, "proximity_pct": 0.1, "dwell_sec": 30.0,
                      "entry_hour_et": 10.0, "ask": 50.02, "bid": 50.00, "rvol": 9.0}}
    o.update(kw)
    return o


def test_parse_row_population_rules():
    assert N.parse_row(_o(), "2026-10-14") is not None
    assert N.parse_row(_o(entry_price=19.99), "2026-10-14") is None
    assert N.parse_row(_o(symbol="TEST"), "2026-10-14") is None
    assert N.parse_row(_o(entry_path="suggest"), "2026-10-14") is None
    assert N.parse_row(_o(entry_time=_t("15:51")), "2026-10-14") is None


def test_parse_row_never_reads_outcome_fields():
    r = N.parse_row(_o(), "2026-10-14")
    flat = repr(r["num"]) + repr(r["cat"])
    assert "extended" not in flat and "rvol" not in r["num"] and "mfe" not in flat


def test_spread_missing_when_crossed():
    f = dict(_o()["features"], ask=50.00, bid=50.01)
    assert N.parse_row(_o(features=f), "2026-10-14")["num"]["spread_bp"] is None
    r = N.parse_row(_o(), "2026-10-14")
    assert r["num"]["spread_bp"] == pytest.approx(0.02 / 50.01 * 1e4)


def test_late_into_square():
    f = dict(_o()["features"], square_since=_t("10:30") + 7 - 61)
    assert N.parse_row(_o(features=f), "2026-10-14")["late_into_square"] == 1.0
    assert N.parse_row(_o(), "2026-10-14")["late_into_square"] == 0.0


def test_reentry_flags_need_prior_exit():
    a = N.parse_row(_o(entry_time=_t("10:00"), exit_time=_t("10:05"), exit_price=50.5), "2026-10-14")
    b = N.parse_row(_o(entry_time=_t("10:03"), exit_time=_t("10:20"), exit_price=49.9), "2026-10-14")   # overlaps a, loses
    c = N.parse_row(_o(entry_time=_t("10:30"), exit_time=_t("10:40")), "2026-10-14")
    N.add_reentry_flags([a, b, c])
    assert a["reentry_same_day"] == 0.0 and b["reentry_same_day"] == 0.0
    assert c["reentry_same_day"] == 1.0
    # c's latest prior exit by entry order is b (exit 10:20 < 10:30): b lost -> 0
    assert c["prior_exit_won"] == 0.0


def test_label_window_excludes_entry_minute():
    et = _t("10:30") + 7
    bars = [(_t("10:30"), 50, 51.0, 49.9, 50), (_t("10:31"), 50, 50.01, 49.9, 50), (_t("10:35"), 50, 50.01, 49.9, 50)]
    assert N.label_from_bars(bars, et, 50.0) is True       # the 10:30 bar's 51.0 is not counted
    bars.append((_t("10:34"), 50, 50.03, 49.9, 50))
    assert N.label_from_bars(bars, et, 50.0) is False      # 50.03 >= 50.025
    assert N.label_from_bars([], et, 50.0) is None


def test_logistic_matches_known_optimum_gradient():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 3))
    y = (X[:, 0] + 0.5 * rng.normal(size=300) > 0).astype(float)
    w, b = N.fit_logistic(X, y, C=1.0)
    p = N.predict(X, w, b)
    assert np.allclose(w + X.T @ (p - y), 0, atol=1e-6)     # dL/dw = 0
    assert abs(np.sum(p - y)) < 1e-6                        # unpenalised intercept
    assert w[0] > 1.0


def test_auc_basic_and_ties():
    assert N.auc([0.9, 0.8, 0.1, 0.2], [1, 1, 0, 0]) == 1.0
    assert N.auc([0.5, 0.5], [1, 0]) == 0.5
    assert N.auc([1, 2], [1, 1]) is None


def test_prep_unseen_category_is_all_zero():
    tr = [N.parse_row(_o(), "2026-10-01"), N.parse_row(_o(features=dict(_o()["features"], source="tight")), "2026-10-01")]
    for r in tr:
        r["reentry_same_day"] = r["prior_exit_won"] = 0.0
    p = N.Prep().fit(tr)
    te = N.parse_row(_o(features=dict(_o()["features"], source="brandnew")), "2026-10-14")
    te["reentry_same_day"] = te["prior_exit_won"] = 0.0
    raw = p._raw(te)
    k = 2 * len(N.NUMERIC)
    assert raw[k:k + len(p.cats["source"])] == [0.0, 0.0]


def test_verdict_precedence():
    ok = {"half_1": {"n": 200, "M2": 3.0}, "half_2": {"n": 150, "M2": 2.5}, "block_fraction": 0.2,
          "auc": 0.65, "auc_ci95": [0.58, 0.7], "M2": 2.6, "t_day": 2.4, "random_share_beaten": 0.97}
    assert N.verdict(661, 414, 0.02, ok) == "PASS"
    assert N.verdict(661, 414, 0.02, dict(ok, block_fraction=0.5)) == "VOID"
    assert N.verdict(661, 414, 0.02, dict(ok, M2=-0.1)) == "FAIL"
    assert N.verdict(661, 414, 0.02, dict(ok, t_day=1.0)) == "PROMISING"
    assert N.verdict(661, 414, 0.02, dict(ok, t_day=1.0, random_share_beaten=0.7)) == "INCONCLUSIVE"


def test_score_test_random_blocks_match_count():
    test = []
    for d in ("2026-10-13", "2026-10-23"):
        for i in range(130):
            test.append({"day": d, "net_bp": float(i % 7) - 3, "never_green": i % 3 == 0})
    p = np.array([1.0 if r["never_green"] else 0.0 for r in test])
    s = N.score_test(test, p, cut=0.5)
    assert s["n_blocked"] == sum(1 for r in test if r["never_green"])
    assert s["auc"] == 1.0 and 0 <= s["random_share_beaten"] <= 1


def test_sklearn_and_newton_agree():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(400, 5))
    y = (X @ np.array([1.0, -0.5, 0.0, 0.3, 0.0]) + rng.normal(size=400) > 0).astype(float)
    w1, b1 = N.fit_logistic(X, y, C=1.0)
    w2, b2 = N.fit_sklearn(X, y)
    assert np.allclose(w1, w2, atol=1e-3) and abs(b1 - b2) < 1e-3
