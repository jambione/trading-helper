import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import breadth_wave as B  # noqa: E402


def test_new_high5():
    h = np.array([[1, 1, 1, 1, 1, 2, 1, 3.0]])
    nh = B.new_high5(h)
    assert nh[0, 5] and not nh[0, 6] and nh[0, 7] and not nh[0, 4]


def test_wave_minutes_share_board_and_live_window():
    n = 20
    band = np.ones((12, n), dtype=bool)
    nh = np.zeros((12, n), dtype=bool)
    nh[:6, 10] = True                       # 6/12 = 50% -> wave at 10 only
    wave, active = B.wave_minutes(band, nh)
    assert wave[10] and not wave[11] and not wave[9]
    assert active[10] and active[12] and not active[13]
    small = np.ones((9, n), dtype=bool)
    w2, _ = B.wave_minutes(small, np.ones((9, n), dtype=bool))
    assert not w2.any()                     # board below 10 names never waves
    nh2 = np.zeros((12, n), dtype=bool); nh2[:5, 10] = True     # 42% < 50%
    assert not B.wave_minutes(band, nh2)[0][10]


def test_traded_gate_and_net(monkeypatch):
    import news_source as ns
    rows = [{"day": "d", "sym": "A", "t": 20, "desk": 14.0, "atr": 30.0},
            {"day": "d", "sym": "B", "t": 20, "desk": 14.0, "atr": 30.0},
            {"day": "d", "sym": "C", "t": 20, "desk": 14.0, "atr": 30.0}]
    cache = {ns.qkey("d", "A", 20): 4.0, ns.qkey("d", "B", 20): 6.5}
    out, gated, missing = B.traded(rows, cache)
    assert len(out) == 1 and gated == 1 and missing == 1
    assert out[0]["net_desk"] == 10.0 and out[0]["net_atr"] == 26.0


def test_verdict():
    assert B.verdict({"n": 5})[0] == "UNDERPOWERED"
    assert B.verdict({"n": 400, "mean_net": 3.0, "t_day": 2.6, "half1": 1, "half2": 1})[0] == "PASS"
    assert B.verdict({"n": 400, "mean_net": 3.0, "t_day": 2.4, "half1": 1, "half2": 1})[0] == "FAIL"
