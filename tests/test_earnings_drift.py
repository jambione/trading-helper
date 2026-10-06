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


# ---------------------------------------------------------------- FIX FIRST 5 split guard
def test_split_guard():
    assert ed.split_suspect(0.51) and ed.split_suspect(-0.75)
    assert not ed.split_suspect(0.5) and not ed.split_suspect(-0.2)


# ---------------------------------------------------------------- FIX FIRST 6 per-event seeding
def test_control_order_is_per_event():
    a = ed.control_order("AAPL", "2024-05-03", 1300, [1300], 5000)
    assert a == ed.control_order("AAPL", "2024-05-03", 1300, [1300], 5000)       # independent of call order
    assert a != ed.control_order("MSFT", "2024-05-03", 1300, [1300], 5000)
    assert len(a) == 12 and all(abs(t - 1300) > 10 for t in a)


# ---------------------------------------------------------------- FIX FIRST 8 power from the larger SE, written first
def test_power_block_uses_larger_se():
    assert ed.power_block(5.0, 12.0)["mde_bp"] == round(2.84 * 12.0, 2)
    assert ed.power_block(12.0, 5.0)["se_used"] == 12.0
    assert math.isnan(ed.power_block(5.0, float("nan"))["mde_bp"])
    assert math.isnan(ed.power_block(float("nan"), 5.0)["mde_bp"])     # order must not matter (max(nan, x) does)


def _fake_rows(n_per_half=40, seed=3):
    import random
    from datetime import date, timedelta
    rng = random.Random(seed)
    rows = []
    for k in range(2 * n_per_half):
        d = date(2020, 1, 6) + timedelta(days=7 * (k % n_per_half)) if k < n_per_half else \
            date(2024, 1, 8) + timedelta(days=7 * (k % n_per_half))
        dd = [(d + timedelta(days=i)).isoformat() for i in range(1, 6)]
        daily = [rng.gauss(0, 100) for _ in range(5)]
        cdaily = [rng.gauss(0, 100) for _ in range(5)]
        rows.append({"sym": f"S{k % 7}", "d0": d.isoformat(), "surprise": "beat", "r0": 0.02, "primary": True,
                     "x5": sum(daily), "raw5": sum(daily), "x1": daily[0], "x20": None, "daily": daily, "ddates": dd,
                     "ctl": {"t": 0, "x5": sum(cdaily), "daily": cdaily, "ddates": dd}})
    return rows


def _fake_P():
    import numpy as np
    return {"syms": np.array([f"S{i}" for i in range(7)]), "status": np.array(["active"] * 7)}


def test_report_writes_power_before_any_mean(tmp_path, monkeypatch):
    import collections
    monkeypatch.setattr(ed, "WORK", str(tmp_path))
    ed.score(_fake_P(), _fake_rows(), collections.Counter({"no_control": 4}), {})
    rep = (tmp_path / "report.md").read_text()
    assert rep.index("## Power") < rep.index("diff_bp") and rep.index("## Power") < rep.index("mean_bp_gross")
    res = __import__("json").loads((tmp_path / "result.json").read_text())
    for h in ("H1", "H2"):
        p = res["power"][h]
        assert p["se_used"] == max(p["se_week"], p["se_cal"]) and p["mde_bp"] == round(2.84 * p["se_used"], 2)


# ---------------------------------------------------------------- FIX FIRST 7 coverage per year
def test_coverage_per_year(monkeypatch):
    import numpy as np
    dates = ["2018-12-31"] + [f"2019-{m:02d}-15" for m in range(1, 13)] + [f"2020-{m:02d}-15" for m in range(1, 13)]
    T = len(dates)
    rc = np.full((T, 3), 20.0)
    rc[:, 2] = 5.0                                     # C never eligible (price)
    adv = np.full((T, 3), 1e8)
    adv[7:, 1] = 1.0                                   # B eligible Jan-Jun 2019 only (6 of 12 sessions)
    P = {"dates": dates, "ix": {d: i for i, d in enumerate(dates)}, "rc": rc, "adv_prev": adv,
         "syms": np.array(["A", "B", "C"])}
    monkeypatch.setattr(ed, "D_LO", "2019-01-15")
    monkeypatch.setattr(ed, "D_HI", "2020-12-15")
    ev = [{"sym": "A", "d0": f"2019-{m:02d}-15"} for m in (2, 5, 8, 11)] + [{"sym": "B", "d0": "2019-04-15"},
                                                                          {"sym": "A", "d0": "2020-02-15"}]
    cov = ed.coverage(P, ev)
    assert cov["2019"]["symbol_years"] == 1.5 and cov["2019"]["matched"] == 5
    assert cov["2019"]["coverage"] == round(5 / 6, 3)
    assert cov["2019"]["eligible_half_year_symbols"] == 2 and cov["2019"]["symbols_lt2_events"] == 1   # B
    assert cov["2020"]["symbols_lt2_events"] == 1 and cov["2020"]["coverage"] == 0.25                   # A, 1 of 4


# ---------------------------------------------------------------- minors: surprise parser
def test_surprise_cases():
    assert ed.surprise("Apple Q3 EPS $2.18 Beats $2.10 Estimate, Sales $90B Miss $91B Estimate") == "beat"
    assert ed.surprise("Acme Q1 Adj. EPS $0.40 Misses $0.45 Estimate, Sales $1B Beat $0.9B Estimate") == "miss"
    assert ed.surprise("Acme Q2 EPS $0.40 In-Line With $0.40 Estimate") == "inline"
    assert ed.surprise("Acme Q2 EPS $0.40 Inline With Estimate") == "inline"
    assert ed.surprise("Acme Q4 EPS $1.05 Beat $1.00 Estimate") == "beat"                 # singular
    assert ed.surprise("Markel Q3 EPS $1,234.50 Beats $1,100.00 Estimate, Sales $4B") == "beat"   # "$1,234" survives
    assert ed.surprise("Acme Q3 EPS $1.00 vs $1.00 Estimate, Sales $5B Beat $4.9B Estimate") == "unclassified"
    assert ed.surprise("Acme Sees Q4 EPS $1.00-$1.10 vs $1.05 Estimate") is None           # guidance excluded
    assert ed.surprise("Acme Raises FY Guidance; Q3 EPS Beats") is None


# ---------------------------------------------------------------- minors: unclassified sample, no_control logged
def test_unclassified_sample_is_seeded(tmp_path):
    ev = [{"sym": f"S{i}", "ts": f"2020-01-{1 + i % 28:02d}", "headline": f"h{i}", "surprise": "unclassified"}
          for i in range(80)] + [{"sym": "B", "ts": "x", "headline": "hb", "surprise": "beat"}]
    a = ed.dump_unclassified(ev, str(tmp_path / "u.txt"))
    b = ed.dump_unclassified(list(reversed(ev)), str(tmp_path / "u2.txt"))
    assert len(a) == 50 and a == b and all(e["surprise"] == "unclassified" for e in a)
    assert (tmp_path / "u.txt").read_text().startswith("# 50 of 80 unclassified")


def test_report_logs_no_control(tmp_path, monkeypatch):
    import collections
    monkeypatch.setattr(ed, "WORK", str(tmp_path))
    ed.score(_fake_P(), _fake_rows(), collections.Counter({"no_control": 4}), {})
    assert "no control (12 candidates tried): 4" in (tmp_path / "report.md").read_text()


# ---------------------------------------------------------------- efficiency: market-hours guard, resumable units
def test_market_hours_guard_window():
    from datetime import datetime
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=ed.ET)  # noqa: E731
    assert ed.in_market_hours(at("2026-10-06T09:00"))           # Tuesday
    assert ed.in_market_hours(at("2026-10-06T16:29"))
    assert not ed.in_market_hours(at("2026-10-06T08:59"))
    assert not ed.in_market_hours(at("2026-10-06T16:30"))
    assert not ed.in_market_hours(at("2026-10-10T12:00"))       # Saturday


def test_news_unit_resumes_from_checkpointed_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "WORK", str(tmp_path))
    (tmp_path / "news").mkdir()
    u = ("AAA", "AAA", "2019-01-01", "2019-02-01")
    pages = {None: ("p1", "t1"), "t1": ("p2", "t2"), "t2": ("p3", None)}
    seen, kill = [], {"t2"}

    def fake(url, params, H):
        tok = params.get("page_token")
        seen.append(tok)
        if tok in kill:
            return None
        h, nxt = pages[tok]
        return {"news": [{"headline": h, "created_at": "x", "id": h}], "next_page_token": nxt}

    assert ed.fetch_unit(u, {}, getf=fake, every=1) is None       # dies on page 3
    kill.clear()
    seen.clear()
    rows = ed.fetch_unit(u, {}, getf=fake, every=1)
    assert seen == ["t2"] and [r["h"] for r in rows] == ["p1", "p2", "p3"]


# ---------------------------------------------------------------- end to end on fakes (no network, no panel file)
def test_events_and_score_end_to_end_on_fakes(tmp_path, monkeypatch):
    import json
    import numpy as np
    import pandas as pd
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2019-01-02", periods=700)]
    T, rng = len(dates), np.random.default_rng(5)
    spy = 300 * np.cumprod(1 + rng.normal(0, 0.01, T))
    c = 50 * np.cumprod(1 + rng.normal(0, 0.015, (T, 2)), axis=0)
    P = {"dates": dates, "ix": {d: i for i, d in enumerate(dates)}, "col": {"AAA": 0, "BBB": 1},
         "c": c, "rc": c.copy(), "v": np.full((T, 2), 5e6), "spy_c": spy, "syms": np.array(["AAA", "BBB"]),
         "status": np.array(["active", "active"])}
    P["adv_prev"] = np.full((T, 2), 1e9)
    monkeypatch.setattr(ed, "WORK", str(tmp_path))
    monkeypatch.setattr(ed, "panel", lambda: P)
    monkeypatch.setattr(ed, "headers", lambda: {})
    monkeypatch.setattr(ed, "market_hours_guard", lambda: None)
    monkeypatch.setattr(ed, "D_LO", dates[200])
    monkeypatch.setattr(ed, "D_HI", dates[650])
    (tmp_path / "news").mkdir()
    S = {"symbols": ["AAA", "BBB"], "name_changes": []}
    json.dump(S, open(tmp_path / "symbols.json", "w"))
    for sym in S["symbols"]:
        arts = []
        for k, t in enumerate(range(210, 640, 63)):
            word = "Beats" if k % 2 == 0 else "Misses"
            arts.append({"h": f"{sym} Q{1 + k % 4} EPS $1.10 {word} $1.00 Estimate", "c": f"{dates[t]}T11:00:00Z", "id": f"{sym}{k}"})
        arts.append({"h": f"{sym} Q2 EPS $1.10 Beats $1.00 Estimate", "c": f"{dates[620]}T19:50:00Z", "id": f"{sym}late"})
        u = (sym, sym, ed.NEWS_START, ed.NEWS_END)
        json.dump(arts, open(ed.unit_file(u), "w"))
    ed.phase_events()
    E = json.load(open(tmp_path / "events.json"))
    assert E["counts"]["excluded_1545_1600"] == 2 and E["earn_t"]["AAA"]

    def fake_get(url, params, H):
        day = params["start"][:10]
        t = P["ix"][day]
        out = {}
        for sym in params["symbols"].split(","):
            j = P["col"][sym]
            out[sym] = [{"t": f"{day}T19:44:00Z", "c": float(c[t - 1, j] * (1.01 if t % 3 else 0.99))}]
        return {"bars": out, "next_page_token": None}

    import pytest
    monkeypatch.setattr(ed, "get", lambda url, params, H: None)       # every minute request fails
    with pytest.raises(SystemExit, match="fetch_fail"):
        ed.phase_minutes_and_score()
    assert not (tmp_path / "result.json").exists()
    monkeypatch.setattr(ed, "get", fake_get)
    ed.phase_minutes_and_score()
    res = json.load(open(tmp_path / "result.json"))
    assert res["score_counts"]["fetch_fail"] == 0 and "coverage" in res and res["power"]["H1"]["n"] >= 0
    assert list(res)[:2] == ["prereg", "power"]
