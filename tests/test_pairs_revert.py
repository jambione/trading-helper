"""pairs_revert: intraday pairs reversion. Synthetic bars and a synthetic warehouse, no market data."""
import math
import os
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pairs_revert as P  # noqa: E402


def _flat(px=100.0):
    return {m: (px, px) for m in range(9 * 60 + 30, 16 * 60)}


def test_spread_needs_both_legs_and_the_anchor():
    a, b = _flat(), _flat()
    a[600] = (101.0, 101.0)
    assert abs(P.spread_at(a, b, 1.0, 600) - math.log(1.01)) < 1e-12
    del b[600]
    assert P.spread_at(a, b, 1.0, 600) is None
    b2 = _flat()
    del b2[P.ANCHOR]
    assert P.spread_at(a, b2, 1.0, 610) is None


def test_pair_return_shorts_the_leader():
    # A ran +1%, then gave back 1%; B flat. Short A earns ~1% of A's notional, over gross 1 + beta.
    g = P.pair_return(1, 1.0, 101.0, 100.0, 50.0, 50.0)
    assert abs(g - (1 - 100 / 101) / 2 * 1e4) < 1e-9
    assert P.pair_return(-1, 1.0, 101.0, 100.0, 50.0, 50.0) < 0


def _diverge(at=620, back=640, size=0.01):
    a, b = _flat(), _flat()
    for m in range(at, 16 * 60):
        px = 100 * (1 + size) if m < back else 100.0
        a[m] = (px, px)
    return a, b


def test_entry_on_2_sigma_exit_on_cross():
    a, b = _diverge()
    tr = P.simulate(a, b, 1.0, 0.004)
    assert len(tr) == 1 and not tr[0]["dropped"]
    t = tr[0]
    assert t["entry_m"] == 620 and t["side"] == 1 and t["reason"] == "cross" and t["exit_m"] == 641
    assert t["gross"] > 0


def test_the_decision_does_not_read_later_bars():
    a, b = _diverge(back=700)
    first = P.simulate(a, b, 1.0, 0.004)[0]["entry_m"]
    for m in range(621, 16 * 60):
        a[m] = (500.0, 500.0)
    assert P.simulate(a, b, 1.0, 0.004)[0]["entry_m"] == first


def test_60_minute_and_1550_exits():
    a, b = _diverge(back=10_000)
    t = P.simulate(a, b, 1.0, 0.004)[0]
    assert t["reason"] == "60 min" and t["exit_m"] == 620 + 61
    a, b = _diverge(at=14 * 60 + 40, back=10_000)
    t = P.simulate(a, b, 1.0, 0.004)[0]
    assert t["reason"] == "60 min" and t["exit_m"] == 14 * 60 + 40 + 61
    a, b = _diverge(at=15 * 60, back=10_000)
    t = P.simulate(a, b, 1.0, 0.004)[0]
    assert t["entry_m"] == 15 * 60 and t["reason"] == "15:50" and t["exit_m"] == P.FLAT
    a, b = _diverge(at=15 * 60 + 1, back=10_000)
    assert P.simulate(a, b, 1.0, 0.004) == []      # no entry after 15:00


def test_no_reentry_until_the_gap_is_under_1_sigma():
    a, b = _diverge(back=10_000)
    tr = P.simulate(a, b, 1.0, 0.004)
    assert len(tr) == 1          # still 2.5 sigma wide after the 60-minute exit: never re-armed
    a, b = _diverge(at=620, back=640)
    for m in range(700, 720):
        a[m] = (101.0, 101.0)
    assert len(P.simulate(a, b, 1.0, 0.004)) == 2


def test_a_missing_minute_is_not_a_trigger():
    a, b = _diverge()
    for m in range(620, 640):
        del b[m]
    tr = P.simulate(a, b, 1.0, 0.004)
    assert all(t["entry_m"] >= 640 for t in tr)


def test_p2_cap_three_pairs_per_name():
    names = list("ABCDEFG")
    c = np.full((7, 7), 0.1)
    c[0, :] = c[:, 0] = 0.9
    np.fill_diagonal(c, np.nan)
    got = P.pick_p2(names, c, n=10, cap=3)
    assert sum(1 for x in got if "A" in x[:2]) == 3


def test_corr_matrix_matches_numpy_on_complete_rows():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(5, 400))
    got = P.corr_matrix(x)
    want = np.corrcoef(x)
    np.fill_diagonal(want, np.nan)
    assert np.allclose(got, want, equal_nan=True)


def test_excess_pairs_sessions():
    arm = [{"day": "d1", "net": 5.0}, {"day": "d1", "net": 3.0}, {"day": "d2", "net": 1.0}]
    plc = [{"day": "d1", "net": 1.0}, {"day": "d3", "net": 9.0}]
    mu, _t, n = P.excess(arm, plc)
    assert n == 1 and mu == 3.0


def _half(**k):
    base = {"sessions": 40, "trades": 400, "net": 2.0, "net_t": 3.0, "ex": 1.0, "ex_t": 3.0, "ex_n": 35,
            "drop_share": 0.0, "conc": {"wo_sessions": 1.0, "wo_pairs": 1.0, "wo_names": 1.0}}
    base.update(k)
    return base


def test_the_words():
    assert P.word(_half(), _half(), 3.0) == "PASS"
    assert P.word(_half(), _half(ex_t=2.0), 3.0) == "PROMISING"
    assert P.word(_half(ex=-0.1), _half(ex_t=2.0), 3.0) == "FAIL"
    assert P.word(_half(), _half(conc={"wo_sessions": 1.0, "wo_pairs": -0.1, "wo_names": 1.0}), 3.0) == "FAIL"
    assert P.word(_half(), _half(trades=299), 3.0) == "UNDERPOWERED"
    assert P.word(_half(), _half(drop_share=0.06), 3.0) == "UNDERPOWERED"


# ── end to end on a synthetic warehouse ─────────────────────────────────────

def _warehouse(tmp, n_days=119, revert=True, seed=3):
    """12 twin pairs (common factor + a pair gap) and 16 loners. With revert=True the gap mean-reverts
    within the pair; with False the gap is a random walk (no edge)."""
    rng = np.random.default_rng(seed)
    pairs = [(f"A{i}", f"B{i}") for i in range(12)]
    loners = [f"L{i}" for i in range(16)]
    syms = sorted([s for p in pairs for s in p] + loners)
    days, d = [], date(2026, 1, 5)
    while len(days) < n_days + 1:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    for f in ("sip", "iex"):
        os.makedirs(os.path.join(tmp, f), exist_ok=True)
    mins = np.arange(570, 960)
    for day in days:
        mkt = np.cumsum(rng.normal(0, 3e-4, len(mins)))
        path = {}
        for a, b in pairs:
            fac = mkt + np.cumsum(rng.normal(0, 1e-3, len(mins)))
            gap = np.zeros(len(mins))
            for k in range(1, len(mins)):
                gap[k] = (0.9 if revert else 1.0) * gap[k - 1] + rng.normal(0, 4e-4)
            path[a], path[b] = fac + gap / 2, fac - gap / 2
        for s in loners:
            path[s] = mkt + np.cumsum(rng.normal(0, 1e-3, len(mins)))
        idx, mm, o, h, low, c, v = [], [], [], [], [], [], []
        for i, s in enumerate(syms):
            px = 100 * np.exp(path[s])
            op = np.r_[px[0], px[:-1]]
            idx += [i] * len(mins)
            mm += list(mins)
            o += list(op)
            h += list(np.maximum(op, px))
            low += list(np.minimum(op, px))
            c += list(px)
            v += [1000.0 + 50 * i] * len(mins)
        arr = {"syms": np.array(syms), "sym_idx": np.array(idx, np.int32), "minute": np.array(mm, np.int16),
               "o": np.array(o, np.float32), "h": np.array(h, np.float32), "l": np.array(low, np.float32),
               "c": np.array(c, np.float32), "v": np.array(v, np.float32)}
        for f in ("sip", "iex"):
            np.savez(os.path.join(tmp, f, f"{day}.npz"), **arr)
    return pairs


def _run(tmp, revert):
    pairs = _warehouse(str(tmp), revert=revert)
    old = (P.WH, P.CUT, P.LAST_DAY, P.MIN_PRESENT, P.P2_N, P.MIN_SESSIONS, P.MIN_TRADES)
    P.WH = str(tmp)
    try:
        P.LAST_DAY = "2099-01-01"
        days = P.usable_days()
        P.CUT = days[(len(days) - 1) // 2]
        P.MIN_PRESENT, P.P2_N, P.MIN_SESSIONS, P.MIN_TRADES = len(days) - 2, 8, 20, 100
        fit_days = [d for d in days if d <= P.CUT]
        declared = [list(p) for p in pairs[:6]]
        F = P.fit(fit_days, days, declared)
        cache = {d: P.load_day(d) for d in fit_days}
        arms = P.build_pairs(F, declared, fit_days, cache)
        md = P.report(arms, F, days)
    finally:
        P.WH, P.CUT, P.LAST_DAY, P.MIN_PRESENT, P.P2_N, P.MIN_SESSIONS, P.MIN_TRADES = old
    return md, F


def test_planted_reversion_reads_pass_and_p2_finds_the_twins(tmp_path):
    md, F = _run(tmp_path / "rev", revert=True)
    head = [x for x in md.splitlines() if x.startswith("P1_declared:")][0]
    assert "P1_declared: PASS" in head, md[:2500]
    assert all(a[0] in "AB" and b[0] in "AB" and a[1:] == b[1:] for a, b, _ in F["p2"])


def test_no_reversion_does_not_pass(tmp_path):
    md, _ = _run(tmp_path / "walk", revert=False)
    head = [x for x in md.splitlines() if x.startswith("P1_declared:")][0]
    assert "PASS" not in head, md[:2500]
