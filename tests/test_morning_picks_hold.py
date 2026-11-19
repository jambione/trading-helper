import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import morning_picks_hold as m  # noqa: E402


def _flat(px=100.0):
    a = np.full(390, px)
    return a.copy(), a.copy(), a.copy(), a.copy()


def test_exit_main_stop_and_close():
    o, h, l, c = _flat()
    c[385] = 102.0
    assert round(m.exit_main(o, h, l, c)) == 200
    l[100] = 96.0
    assert round(m.exit_main(o, h, l, c)) == -300
    o[100], l[100] = 95.0, 94.0          # gapped through the stop: fill at the open
    assert round(m.exit_main(o, h, l, c)) == -500


def test_giveback_locks_half_the_peak():
    o, h, l, c = _flat()
    for a in (o, h, l, c):
        a[60:] = 103.0
    h[60] = 104.0
    l[80] = 101.5                        # peak +4% -> floor +2%, touched at bar 80
    assert round(m.exit_giveback(o, h, l, c)) == 200


def test_rankers_and_eligibility():
    f = lambda **k: {"px": 50, "pct": 3, "dv30": 9e6, "pace": 0.2, "rs": 50, "holding": True, "gap": 3, "unfilled": True, **k}  # noqa: E731
    rows = [{"sym": "A", "f": f(pace=0.5, rs=10, gap=2.5)}, {"sym": "B", "f": f(pace=0.1, rs=90, holding=False, gap=5)},
            {"sym": "C", "f": f(pace=0.3, rs=40, gap=1.0)}]
    assert m.rank(rows, "VOL") == ["A", "C", "B"]
    assert m.rank(rows, "RS") == ["C", "A"]
    assert m.rank(rows, "GAP") == ["B", "A"]
    assert m.eligible(f()) and not m.eligible(f(px=15)) and not m.eligible(f(dv30=1e6)) and not m.eligible(f(pct=1.0))


def test_picks_skip_unquoted_and_verdict_bar():
    d = {"day": "2026-05-01", "rows": [{"sym": s, "gross": 10.0} for s in "ABC"], "ranked": {"VOL": ["A", "B", "C"]}}
    picks, sk = m.picks_for(d, "VOL", 2, {"2026-05-01|A|29": None, "2026-05-01|B|29": 3.0, "2026-05-01|C|29": 4.0})
    assert [r["sym"] for r, _ in picks] == ["B", "C"] and sk == 1
    good = {"net": 12, "diff": 11, "diff_t": 2.6, "half1": 5, "half2": 6, "drop_best": 3}
    assert m.verdict(good, {"net": 1, "diff": 1})[0] == "PASS"
    assert m.verdict({**good, "diff_t": 2.4}, {"net": 1, "diff": 1})[0] == "FAIL"
