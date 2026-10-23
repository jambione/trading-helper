import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import etf_bars as B  # noqa: E402


def test_power_bar_rules():
    assert B.power_bar((100, 101.0, 99.9, 100.95, 600), 0.5, 1)          # range 1.1 >= 1.0, body 0.95, top 20%
    assert not B.power_bar((100, 101.0, 99.9, 100.95, 600), 0.6, 1)      # range < 2 x ref
    assert not B.power_bar((100, 101.0, 99.9, 100.4, 600), 0.5, 1)       # body too small / not top 20%
    assert B.power_bar((101, 101.1, 100.0, 100.05, 600), 0.5, -1)
    assert not B.power_bar((100, 101.0, 99.9, 100.95, 600), None, 1)


def test_size_ref_needs_five_and_resets():
    s = B.SizeRef()
    for i in range(4):
        s.add([(0, 1.0 + i, 0.0, 0.5, 571)])
    assert s.ref(0) is None
    s.add([(0, 3.0, 0.0, 0.5, 571)])
    assert s.ref(0) == 3.0                                      # median of 1, 2, 3, 4, 3
    s.reset()
    assert s.ref(0) is None


def test_size_ref_counts_sessions_not_bars():
    s = B.SizeRef()
    for _ in range(5):
        s.add([None])
    for _ in range(5):
        s.add([(0, 2.0, 0.0, 1, 571)])
    assert s.ref(0) == 2.0
    s.add([None])                      # 11th session: oldest drops; 5 present still
    assert s.ref(0) == 2.0
    for _ in range(5):
        s.add([None])
    assert s.ref(0) is None            # only 4 present in the last 10 sessions


def test_pm_loose_combined_power_bar():
    import numpy as np
    import pm_power_bar as P
    refs = {n: B.SizeRef() for n in (1, 2, 3)}
    quiet = [(100.0, 100.1, 99.9, 100.0, 571 + 2 * k) for k in range(30)]
    for _ in range(6):
        for n, ref in refs.items():
            ref.add(P.combos(quiet, n))
    bars = list(quiet)
    # two green bars off the low 99.0: neither alone is 1.5x, together they are
    bars[5] = (99.05, 99.25, 99.00, 99.20, 581)       # range .25 < 1.5 x .20
    bars[6] = (99.20, 99.45, 99.18, 99.40, 583)       # range .27 alone; .45 combined
    r = np.array([[m, 100, 100, 100, 100, 1] for m in range(570, 581)], dtype=float)
    sig = P.loose_candidate(r, bars, refs, 101.0, 99.0, 1)
    assert sig is not None and sig["n"] == 2 and sig["end"] == 583 and abs(sig["low"] - 99.0) < 1e-9
    r_broken = np.array([[575, 99, 99, 98.9, 98.95, 1]], dtype=float)   # an RTH close below PML first
    assert P.loose_candidate(r_broken, bars, refs, 101.0, 99.0, 1) is None


def _refs_quiet():
    import pm_power_bar as P
    refs = {n: B.SizeRef() for n in (1, 2, 3)}
    quiet = [(100.0, 100.1, 99.9, 100.0, 571 + 2 * k) for k in range(30)]
    for _ in range(6):
        for n, ref in refs.items():
            ref.add(P.combos(quiet, n))
    return refs, quiet


def test_pm_smallest_n_wins_at_same_k():
    import numpy as np
    import pm_power_bar as P
    refs, bars = _refs_quiet()
    bars = list(bars)
    bars[5] = (99.05, 99.25, 99.00, 99.20, 581)     # small alone; with bar 6 a green 2-bar window also qualifies
    bars[6] = (99.05, 99.40, 99.00, 99.35, 583)     # alone: range .40 >= .30, body .30 = 75%, green
    r = np.array([[m, 100, 100, 100, 100, 1] for m in range(570, 581)], dtype=float)
    sig = P.loose_candidate(r, bars, refs, 101.0, 99.0, 1)
    assert sig["n"] == 1 and sig["end"] == 583


def test_pm_break_inside_window_blocks_smaller_n_only():
    import numpy as np
    import pm_power_bar as P
    refs, bars = _refs_quiet()
    bars = list(bars)
    bars[5] = (99.05, 99.25, 99.00, 99.20, 581)      # 09:40-09:41, too small alone
    bars[6] = (99.10, 99.50, 99.05, 99.45, 583)      # 09:42-09:43, a power bar alone
    # one 1-minute close below PML at 09:41: inside the n=2 window (starts 09:40), before the n=1 window (09:42)
    r = np.array([[m, 99.5, 99.5, 98.9, 98.95 if m == 581 else 99.5, 1] for m in range(570, 583)], dtype=float)
    sig = P.loose_candidate(r, bars, refs, 101.0, 99.0, 1)
    assert sig is not None and sig["n"] == 2 and sig["end"] == 583
