"""together_sweep: grid flags, earliest onset, end-to-end on a synthetic warehouse. No market data."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
sys.path.insert(0, os.path.dirname(__file__))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402
import together_sweep as T  # noqa: E402
from test_prescreen_turn import _day  # noqa: E402


def test_cell_flag_needs_all_four_conditions():
    n = 30
    fast = np.array([-95.0] * 20 + [-90, -85, -80, -75, -70, -65, -60, -55, -50, -45], float)
    slow = np.array([-65.0] * 20 + [-64, -63, -62, -61, -60, -59, -58, -57, -56, -55], float)
    cell = (-80.0, -50.0, 20.0, 3, -30.0)
    fl = T.cell_flags(fast, slow, cell)
    first = int(np.flatnonzero(fl)[0])
    assert abs(fast[first] - slow[first]) <= 20 and fast[first] <= -30          # close + early at the first bar
    assert not T.cell_flags(fast, slow, (-80.0, -50.0, 1.0, 3, -30.0)).any()    # never within 1 point
    assert not T.cell_flags(fast, slow, (-99.0, -50.0, 20.0, 3, -30.0)).any()   # dip never reached -99
    flat = slow.copy(); flat[20:] = -65.0
    assert not T.cell_flags(fast, flat, cell).any()                            # slow not rising: not together
    assert len(fl) == n


def test_onset_is_the_earliest_bar_with_refractory():
    m = list(range(600, 640))
    fl = np.zeros(40, bool); fl[[5, 6, 7, 12, 30]] = True
    assert T.onsets(m, fl) == [605, 630]                                       # 612 inside the 15-min refractory


def test_grid_and_day_job_end_to_end(tmp_path, monkeypatch):
    assert len(T.CELLS) == 243 and T.BASE in T.CELLS
    for f in ("iex", "sip"):
        os.makedirs(tmp_path / f)
    for k, day in enumerate(("2026-10-05", "2026-10-06")):
        for f in ("iex", "sip"):
            _day(tmp_path / f / f"{day}.npz", day, k)
    monkeypatch.setattr(P, "WH", str(tmp_path))
    monkeypatch.setattr(E, "WARM_LINES", True)
    day, acc = T.day_job(("2026-10-06", "2026-10-05"))
    cnt = acc.pop("_counts")
    assert day == "2026-10-06" and set(acc) == set(T.CELLS) and cnt["sampled"] == 3
    per_day = {"2026-10-06": acc, "2026-10-07": acc}
    s = T.summarize(per_day, ["2026-10-06", "2026-10-07"], "2026-10-06")
    md = T.report(s, "2026-10-06", 2)
    assert "Trending together" in md


def test_no_fake_onset_at_window_start():
    f = -90 + 0.5 * np.arange(60)
    s = -85 + 0.5 * np.arange(60)
    m = list(range(570, 630))
    assert 590 not in T.onsets(m, T.cell_flags(f, s, T.BASE))          # state began at the open, not at 09:50
