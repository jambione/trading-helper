"""Pure rules of tools/studies/earnings_drift.py (prereg docs/studies/earnings_drift_prereg.json).

Written for the skeptic's 2026-10-06 review: each test pins one finding so a regression reads as that finding.
No network, no panel: only the parsers, the walk and the statistics helpers.
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

import earnings_drift as ed  # noqa: E402


# ---------------------------------------------------------------- F2 t_used sign rule
def test_t_used_is_signed_min():
    assert ed.t_used(3.0, 2.5) == 2.5
    assert ed.t_used(3.0, -2.5) == -2.5          # old rule: min |t| = 2.5 with the mean's sign -> +2.5, a false pass
    assert ed.t_used(-1.0, 4.0) == -1.0


def test_t_used_nan_fails():
    assert math.isnan(ed.t_used(float("nan"), 3.0))
    assert math.isnan(ed.t_used(3.0, float("nan")))
    assert not (ed.t_used(3.0, float("nan")) >= 2)


# ---------------------------------------------------------------- F3 NaN inside the hold
def _series(n=300, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    spy = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    stk = 50 * np.cumprod(1 + rng.normal(0, 0.015, n))
    spy_r = np.r_[np.nan, spy[1:] / spy[:-1] - 1]
    return stk.reshape(-1, 1), spy, spy_r, [f"d{i}" for i in range(n)]


def test_excess_needs_every_close_in_the_hold():
    c, spy, spy_r, dates = _series()
    ok = ed.excess_at(c, spy, spy_r, dates, 0, 200, 5)
    assert ok is not None and all(math.isfinite(v) for v in ok["daily"])
    c[202, 0] = float("nan")                       # endpoints fine, a hole mid-hold
    assert ed.excess_at(c, spy, spy_r, dates, 0, 200, 5) is None


# ---------------------------------------------------------------- F1 rename chain walk (FISV)
FISV_NC = [
    {"old_symbol": "FI", "new_symbol": "FISV", "old_cusip": "337738108", "new_cusip": "337738108", "process_date": "2025-11-11"},
    {"old_symbol": "FISV", "new_symbol": "FI", "old_cusip": "337738108", "new_cusip": "337738108", "process_date": "2023-06-07"},
    {"old_symbol": "FI", "new_symbol": "XPRO", "old_cusip": "N3144W105", "new_cusip": "N3144W999", "process_date": "2021-10-04"},
]


def test_rename_walk_fisv_skips_franks_international():
    segs = ed.rename_segments("FISV", FISV_NC)
    assert segs == [("FISV", "FISV", "2025-11-11", ed.NEWS_END),
                    ("FISV", "FI", "2023-06-07", "2025-11-11"),
                    ("FISV", "FISV", ed.NEWS_START, "2023-06-07")]
    # FI before 2021-10-04 was Frank's International: never on Fiserv's chain
    assert not any(t == "FI" and a < "2023-06-07" for _, t, a, _ in segs)


def test_rename_walk_drops_cusip_change_and_xpro_owner():
    # XPRO's own link changed CUSIP here, so XPRO keeps only its own ticker
    assert ed.rename_segments("XPRO", FISV_NC) == [("XPRO", "XPRO", ed.NEWS_START, ed.NEWS_END)]
    assert ed.rename_segments("AAPL", FISV_NC) == [("AAPL", "AAPL", ed.NEWS_START, ed.NEWS_END)]


def test_unit_files_are_per_segment():
    S = {"symbols": ["FISV"], "name_changes": FISV_NC}
    files = {os.path.basename(ed.unit_file(u)) for u in ed.news_units(S)}
    assert len(files) == 3 and f"FISV__FI__2023-06-07__2025-11-11.json" in files


# ---------------------------------------------------------------- FIX FIRST 1 burst rule
def test_burst_rule():
    B = lambda *xs: [(i, h, w) for i, (h, w) in enumerate(xs)]  # noqa: E731
    assert ed.burst_word(B(("X Q1 EPS", "unclassified"))) == "unclassified"
    assert ed.burst_word(B(("X Q1 EPS Beats", "beat"), ("X Q1 EPS vs", "unclassified"))) == "beat"
    assert ed.burst_word(B(("X Q1 GAAP EPS Misses", "miss"), ("X Q1 Adj. EPS Beats", "beat"))) == "beat"
    assert ed.burst_word(B(("X Q1 GAAP EPS Misses", "miss"), ("X Q1 EPS Beats", "beat"))) == "unclassified"
    # an unclassified Adj. headline does not decide (nor block) the conflict
    assert ed.burst_word(B(("X Q1 Adj. EPS vs", "unclassified"), ("X Q1 EPS Misses", "miss"),
                           ("X Q1 Adj. EPS Beats", "beat"))) == "beat"
    assert ed.burst_word(B(("X Q1 Adj. EPS vs", "unclassified"), ("X Q1 EPS Misses", "miss"),
                           ("X Q1 EPS Beats", "beat"))) == "unclassified"


# ---------------------------------------------------------------- FIX FIRST 2 D0 mapping
def test_d0_mapping():
    from datetime import datetime
    S = ["2025-07-24", "2025-07-25", "2025-07-28"]           # Thu, Fri, Mon
    ss = set(S)
    at = lambda d, hm: datetime.fromisoformat(f"{d}T{hm}:00").replace(tzinfo=ed.ET)  # noqa: E731
    assert ed.d0_of(at("2025-07-24", "07:00"), S, ss) == "2025-07-24"     # pre-market -> same session
    assert ed.d0_of(at("2025-07-24", "12:00"), S, ss) == "2025-07-24"     # RTH -> same
    assert ed.d0_of(at("2025-07-24", "15:44"), S, ss) == "2025-07-24"
    assert ed.d0_of(at("2025-07-24", "15:45"), S, ss) == "late_rth"
    assert ed.d0_of(at("2025-07-24", "15:50"), S, ss) == "late_rth"       # excluded: after R0 and the MOC cutoff
    assert ed.d0_of(at("2025-07-24", "16:00"), S, ss) == "2025-07-25"     # after the close -> next
    assert ed.d0_of(at("2025-07-25", "16:05"), S, ss) == "2025-07-28"     # Friday evening -> Monday
    assert ed.d0_of(at("2025-07-26", "15:50"), S, ss) == "2025-07-28"     # weekend -> next session, not excluded
    assert ed.d0_of(at("2025-07-28", "16:30"), S, ss) is None             # past the panel


# ---------------------------------------------------------------- FIX FIRST 3 control exclusion
def test_control_candidates_avoid_every_earnings_headline():
    c = ed.control_candidates(1000, [1000, 1040], 5000)
    assert all(abs(t - 1000) > 10 and abs(t - 1040) > 10 for t in c)
    assert 1030 not in c and 1050 not in c and 1010 not in c and 1011 in c and 1029 in c and 1051 in c


# ---------------------------------------------------------------- FIX FIRST 4 news fetch-fail abort
def test_news_fail_check_aborts_over_two_percent():
    import pytest
    S = {"symbols": [f"S{i}" for i in range(100)], "name_changes": []}
    two = {ed.unit_file(("S0", "S0", ed.NEWS_START, ed.NEWS_END)), ed.unit_file(("S1", "S1", ed.NEWS_START, ed.NEWS_END))}
    assert ed.news_fail_check(S, exists=lambda f: f not in two) == ["S0", "S1"]
    three = two | {ed.unit_file(("S2", "S2", ed.NEWS_START, ed.NEWS_END))}
    with pytest.raises(SystemExit):
        ed.news_fail_check(S, exists=lambda f: f not in three)


# ---------------------------------------------------------------- FIX FIRST 4 + efficiency: minute batches
def test_pick_1545_prefers_1544_else_last_in_window():
    bar = lambda hm, c: {"t": f"2025-06-02T{hm}:00Z", "c": c}  # noqa: E731  (UTC; 19:44Z = 15:44 ET)
    assert ed.pick_1545([bar("19:35", 1.0), bar("19:44", 2.0), bar("19:45", 3.0)]) == 2.0
    assert ed.pick_1545([bar("19:40", 1.5), bar("19:36", 1.0), bar("19:45", 3.0)]) == 1.5
    assert ed.pick_1545([bar("19:34", 1.0), bar("19:45", 3.0)]) is None


def test_fetch_minutes_batches_by_day_and_never_caches_failures():
    calls = []

    def fake(url, params, H):
        calls.append(params)
        if params["start"].startswith("2025-06-03"):
            return None                                       # this day's request fails after retries
        return {"bars": {"AAA": [{"t": "2025-06-02T19:44:00Z", "c": 10.0}]}, "next_page_token": None}

    cache = {}
    keys = {("AAA", "2025-06-02"), ("BBB", "2025-06-02"), ("AAA", "2025-06-03")}
    fails = ed.fetch_minutes(cache, {}, keys, getf=fake)
    assert len(calls) == 2 and calls[0]["symbols"] == "AAA,BBB" and calls[0]["limit"] == 10000
    assert calls[0]["feed"] == "sip" and calls[0]["adjustment"] == "raw"
    assert cache == {"AAA|2025-06-02": 10.0, "BBB|2025-06-02": None}   # BBB: fetched, no bars -> cached None
    assert fails == {"AAA|2025-06-03"} and "AAA|2025-06-03" not in cache
    calls.clear()
    ed.fetch_minutes(cache, {}, keys, getf=fake)                        # rerun retries only the failure
    assert len(calls) == 1 and calls[0]["symbols"] == "AAA"
