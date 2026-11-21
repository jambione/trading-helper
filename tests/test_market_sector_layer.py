import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import market_sector_layer as m  # noqa: E402


def test_ret15_uses_only_past_and_stale_rule():
    c = np.full(390, np.nan); c[100] = 10.0; c[85] = 9.9
    assert abs(m.ret15(c, 100) - (10 / 9.9 - 1)) < 1e-12
    assert m.ret15(c, 103) is None                      # last bar 3 minutes old -> unknown
    assert m.ret15(c, 99) is None or m.ret15(c, 99) != m.ret15(c, 100)   # never reads bar 100 at t=99


def test_peer_lift_matches_trigger_window_and_spy_sign():
    f = lambda sym, t, g, R1, spy=0.001: {"day": "d", "trig": "F2", "sym": sym, "t": t, "g": g, "R1": R1, "SECT": None,
                                          "vt": 1, "gb": 1, "spy": spy, "sec": "XLK"}
    rows = [f("A", 100, 10.0, True), f("B", 110, 4.0, False), f("C", 130, 0.0, False), f("D", 105, -2.0, False, spy=-0.001)]
    out, dropped = m.peer_lift(rows, lambda x: x["R1"], lambda x: not x["R1"])
    assert dropped == 0 and len(out) == 1 and out[0]["lift"] == 6.0     # only B: C is > 15 min away, D has the other SPY sign
