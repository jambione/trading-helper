import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import squeeze_intraday as m  # noqa: E402


def test_report_respects_publication_lag():
    cal = m.calendar()
    s = ["2026-09-15", "2026-09-30", "2026-10-15"]
    assert m.report_for("2026-10-14", s, cal) == "2026-09-15"      # 9/30 + 10 sessions = 10/14: not usable ON 10/14
    assert m.report_for("2026-10-15", s, cal) == "2026-09-30"
    assert m.report_for("2026-10-13", s, cal) == "2026-09-15"


def test_groups_top_decile_and_below_70th():
    g = m.groups({f"S{i}": float(i) for i in range(1, 21)})
    assert g["S20"][0] == "HIGH" and g["S19"][0] == "HIGH"
    assert g["S1"][0] == "NORMAL" and g["S14"][0] == "NORMAL"
    assert g["S16"][0] == "MID"


def test_breakout_needs_new_high_gain_and_volume():
    o = np.full(390, 100.0); h = o + 0.1; l = o - 0.1; c = o.copy(); v = np.full(390, 1000.0)
    c[40] = 104.0; h[40] = 104.1
    assert m.breakout(o, h, l, c, v, 100.0, 200_000.0) == 40          # cum vol 41k >= 1.5*200k*41/390 = 31.5k
    assert m.breakout(o, h, l, c, v, 100.0, 400_000.0) is None
    c[40] = 102.0
    assert m.breakout(o, h, l, c, v, 100.0, 200_000.0) is None


def test_diff_stats_and_cluster_se():
    rng = np.random.default_rng(1)
    hi = [{"day": f"d{i % 10}", "net": 10.0 + rng.normal()} for i in range(60)]
    no = [{"day": f"d{i % 10}", "net": 0.0 + rng.normal()} for i in range(60)]
    d = m.diff_stats(hi, no)
    assert d["diff"] > 9 and d["t"] > 2.5
