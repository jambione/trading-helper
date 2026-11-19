import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import leaders_source as L  # noqa: E402


def test_top_mask_ranks_within_band_per_minute():
    pct = np.array([[5.0, 1.0], [4.0, 9.0], [3.0, 8.0], [9.0, 7.0]])
    band = np.array([[True, True], [True, True], [True, True], [False, True]])
    m = L.top_mask(pct, band, n=2)
    assert m[:, 0].tolist() == [True, True, False, False]       # row 3 is out of band
    assert m[:, 1].tolist() == [False, True, True, False]


def test_catalyst_at():
    assert L.catalyst_at(30, 10, cumv_t=0.0, prev_v=1e6, gap_pct=0.5) == ["news"]
    assert L.catalyst_at(5, 10, cumv_t=0.0, prev_v=1e6, gap_pct=0.5) == []            # story after t
    assert L.catalyst_at(38, None, cumv_t=2e5, prev_v=1e6, gap_pct=None) == ["vol"]   # 2e5 / (1e6 x 0.1) = 2x
    assert L.catalyst_at(38, None, cumv_t=0.0, prev_v=1e6, gap_pct=2.5) == ["gap"]


def test_gated_uses_the_gate(monkeypatch):
    import news_source as ns
    rows = [{"day": "d", "sym": "A", "t": 1, "gross": 10.0}, {"day": "d", "sym": "B", "t": 1, "gross": 10.0}]
    cache = {ns.qkey("d", "A", 1): 4.0, ns.qkey("d", "B", 1): 12.0}
    assert len(L.gated(rows, cache, 15.0)) == 2 and len(L.gated(rows, cache, 6.0)) == 1
