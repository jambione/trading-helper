import os
import sys
from collections import Counter, defaultdict, deque

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import periodicity as P  # noqa: E402


def full_day(px=100.0, step=0.01):
    return np.array([[m, px + step * (m - 570), px + step * (m - 570) + 0.02, px + step * (m - 570) - 0.02,
                      px + step * (m - 570) + 0.005, 100] for m in range(570, 960)], dtype=float)


def test_slot_fields_shape_and_returns():
    f = P.slot_fields(full_day())
    assert len(f["hist"]) == 13 and len(f["ent"]) == 13
    assert abs(f["ent"][1] - (100 + 0.01 * 30)) < 1e-9          # 10:00 bar open
    exit_c = 100 + 0.01 * (629 - 570) + 0.005                   # 10:29 close
    assert abs(f["ret"][1] - (exit_c / f["ent"][1] - 1)) < 1e-12


def test_missing_slot_start_bar_skips_entry_not_exit():
    r = full_day()
    r = r[r[:, 0] != 600]                                       # no 10:00 bar
    f = P.slot_fields(r)
    assert f["ent"][1] is None and f["ret"][1] is None
    r = full_day()
    r = r[r[:, 0] != 629]                                       # no 10:29 bar: exit at 10:28 close
    assert P.slot_fields(r)["ret"][1] is not None


def test_signal_needs_ten_sessions_and_top_decile():
    hist = defaultdict(lambda: [deque(maxlen=20) for _ in range(13)])
    for i in range(9):
        hist["A"][1].append(0.01)
    assert P.signals(hist, "A", 1) is None
    hist["A"][1].append(0.01)
    assert P.signals(hist, "A", 1) == 0.01
    f, st = {}, Counter()
    for j in range(30):
        s = f"S{j:02d}"
        for _ in range(10):
            hist[s][1].append(j / 1000)
        f[s] = {"ent": [None] + [50.0] * 12, "ret": [None] + [0.0] * 12, "hist": [None] * 13}
    f["S29"]["ret"][1] = 0.003                                  # the top-SIG name earns 30 bp in slot 2
    d = P.day_summary("2026-06-01", f, hist, st)
    assert d is not None and d["k"] >= 3
