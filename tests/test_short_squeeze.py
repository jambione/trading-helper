"""Pure helpers of short_squeeze: ETF filter, calendar, breakout, beta, clustered / NW SEs, verdicts. No data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import short_squeeze as Q  # noqa: E402


def test_etf_filter_whole_words():
    assert Q.is_etf_like("iShares Core S&P 500 ETF") and Q.is_etf_like("SPDR Gold Trust")
    assert not Q.is_etf_like("Trustmark Corporation") and not Q.is_etf_like("Fundamental Global Inc.")
    assert not Q.is_etf_like("Realty Income Trust")        # amended_4 (B): REITs named 'Trust' stay in
    assert Q.is_etf_like("Global X Funds") and not Q.is_etf_like("Invesco Ltd.")
    for op in ("Invesco Ltd.", "WisdomTree, Inc.", "Northern Trust Corporation", "Amplify Energy Corp."):
        assert not Q.is_etf_like(op), op
    for etf in ("Invesco QQQ Trust, Series 1", "Vanguard Short-Term Corporate Bond ETF",
                "Invesco Optimum Yield Diversified Commodity Strategy No K-1 ETF", "Grayscale Bitcoin Trust"):
        assert Q.is_etf_like(etf), etf
    assert Q.is_non_common("Acme Corp 7% Pfd Series A") and Q.is_non_common("Blank Check Acq Corp Units")
    assert Q.is_non_common("Foo Inc WT exp 2027") and not Q.is_non_common("Unity Software Inc.")
    assert Q.is_etf_like("Some Co", "VanEck Semiconductor ETF")                  # the Alpaca name counts too
    assert not Q.is_etf_like("Consumer Portfolio Services") and not Q.is_etf_like("Index Holdings Corp")
    assert Q.alpaca_symbol("BRKB", {"BRK.B": "x"}) == "BRK.B" and Q.alpaca_symbol("AAPL", {"AAPL": "x"}) == "AAPL"


def test_trading_days_and_publication():
    days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"]
    assert Q.add_trading_days(days, "2026-09-30", 2) == "2026-10-02"
    assert Q.add_trading_days(days, "2026-10-03", 0) == "2026-10-05"     # a weekend settles forward
    assert Q.add_trading_days(days, "2026-10-01", 10) is None


def test_breakout_rule():
    c = [100.0] * 20 + [103.5]
    v = [1e6] * 20 + [2.5e6]
    assert Q.is_breakout(c, v, 20)
    assert not Q.is_breakout(c[:-1] + [102.0], v, 20)                 # < +3%
    assert not Q.is_breakout(c, v[:-1] + [1.5e6], 20)                 # < 2x volume
    c2 = [100.0] * 10 + [110.0] + [100.0] * 9 + [103.5]
    assert not Q.is_breakout(c2, v, 20)                                # not a 20-day high close
    assert not Q.is_breakout(c, v, 19)                                 # not enough history


def test_beta():
    rng = random.Random(1)
    rm = [rng.gauss(0, 0.01) for _ in range(60)]
    rn = [1.5 * x + rng.gauss(0, 0.001) for x in rm]
    assert abs(Q.beta(rn, rm) - 1.5) < 0.05 and Q.beta(rn[:10], rm[:10]) is None


def test_series_stats_and_verdicts():
    rng = random.Random(2)
    ev = [{"d0": f"2021-{(k % 12) + 1:02d}-{(k % 27) + 1:02d}", "sym": f"S{k % 40}", "diff": 0.008 + rng.gauss(0, 0.01)}
          for k in range(600)]
    st = Q.series_stats(ev)
    assert st["n"] == 600 and abs(st["mean"] - 0.008) < 0.002 and st["se"] >= st["se_week"]
    assert Q.mde(st) < 0.005 and Q.half_verdict(st, True) == "PASS"
    assert Q.half_verdict(st, False) == "FAIL"                         # powered, a guard fails -> FAIL
    flat = Q.series_stats([dict(e, diff=e["diff"] - 0.008) for e in ev])
    assert Q.half_verdict(flat, True) == "FAIL"
    assert Q.half_verdict(dict(st, n=100), True) == "UNDERPOWERED"
    assert Q.final(["PASS", "PASS"]).startswith("PASS") and Q.final(["PASS", "FAIL"]) == "FAIL"
    assert Q.final(["UNDERPOWERED", "PASS"]) == "UNDERPOWERED"


def test_driscoll_kraay_and_clustered_se():
    vals = [1.0, -1.0] * 20
    dates = [f"2021-0{1 + (k % 6)}-1{k % 9}" for k in range(40)]
    assert Q.driscoll_kraay_se(vals, dates) is not None and Q.clustered_se([1.0, 2.0], ["a", "a"]) is None
    assert Q.monday_of("2026-10-23") == "2026-10-19"
    assert Q.spearman_avg([1, 1, 2, 3], [1, 1, 2, 3]) == 1.0 and Q.avg_ranks([5, 5, 1]) == [1.5, 1.5, 0.0]


def _market(n_days=330, n_syms=60, seed=3):
    from datetime import date, timedelta
    rng = random.Random(seed)
    days, d = [], date(2019, 9, 2)
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    spy_c = [300.0]
    for _ in days[1:]:
        spy_c.append(spy_c[-1] * (1 + rng.gauss(0, 0.008)))
    bars = {"SPY": {"d": days, "o": spy_c, "c": spy_c, "v": [1e8] * n_days}}
    for k in range(n_syms):
        c = [50.0]
        for t in range(1, n_days):
            c.append(c[-1] * (1 + 1.0 * (spy_c[t] / spy_c[t - 1] - 1) + rng.gauss(0, 0.004)))
        bars[f"S{k}"] = {"d": list(days), "o": list(c), "c": c, "v": [1e6] * n_days}
    return days, bars


def _si():
    return {"20200131": {f"S{k}": {"dtc": float(k + 1), "short": 1e6, "chg": 1.0, "adv": 1e6, "name": f"Company {k} Inc",
                                   "flagged": False, "run": f"S{k}"} for k in range(60)}}


def test_runs_split_on_name_change_and_gaps(tmp_path, monkeypatch):
    rows = {"20200115": "Old Co", "20200131": "Old Co", "20200214": "New Co Inc"}
    for d, nm in rows.items():
        (tmp_path / f"shrt{d}.csv").write_text(
            "symbolCode|issueName|marketClassCode|daysToCoverQuantity|currentShortPositionQuantity|changePercent|"
            "averageDailyVolumeQuantity|revisionFlag|stockSplitFlag\n"
            f"XYZ|{nm}|NYSE|2.0|100|1.0|1000||\nETFZ|Funds|ARCA|1|1|1|1||\n")
    monkeypatch.setattr(Q, "SI_DIR", str(tmp_path))
    si = Q.load_reports_raw()
    assert si["20200115"]["XYZ"]["run"] == si["20200131"]["XYZ"]["run"] == "XYZ@20200115"
    assert si["20200214"]["XYZ"]["run"] == "XYZ@20200214" and "ETFZ" not in si["20200131"]
    r = Q.runs_of(si)
    assert r["XYZ@20200115"]["last"] == "20200131"


def test_build_events_and_match():
    days, bars = _market()
    t = days.index("2020-03-02")
    for sym, after in (("S59", 0.03), ("S0", 0.0), ("S1", 0.0)):     # S59 is HIGH-DTC, S0 / S1 NORMAL
        b = bars[sym]
        b["c"][t] = max(b["c"][t - 20:t]) * 1.05
        b["v"][t] = 3e6
        for k in range(t + 1, len(days)):                              # S59 keeps rising after the breakout
            b["c"][k] = b["c"][k - 1] * (1 + (after / 5 if k <= t + 6 else 0) + (bars["SPY"]["c"][k] / bars["SPY"]["c"][k - 1] - 1))
    si = _si()
    st = Q.Counter()
    ev = Q.build_events(si, bars, bars, bars["SPY"], Q.PUB_LAG, st)
    by = {e["sym"]: e for e in ev}
    assert by["S59"]["grp"] == "HIGH" and by["S0"]["grp"] == "NORMAL" and by["S59"]["d0"] == "2020-03-02"
    assert by["S59"]["abn"] > 0.02 and abs(by["S0"]["abn"]) < 0.02 and "S50" not in by   # 70-90th pct excluded
    paired, info = Q.match(ev)
    assert (len(paired) == 1 and paired[0]["n_matched"] >= 1) or info["unmatched_high"] == 1
    if paired:
        assert paired[0]["diff"] > 0.01


def test_publication_lag_blocks_early_trigger():
    days, bars = _market()
    t = days.index("2020-02-07")                                       # before settlement 1/31 + 10 trading days
    b = bars["S59"]
    b["c"][t] = max(b["c"][t - 20:t]) * 1.05
    b["v"][t] = 3e6
    si = _si()
    ev = Q.build_events(si, bars, bars, bars["SPY"], Q.PUB_LAG, Q.Counter())
    assert all(e["d0"] > "2020-02-14" for e in ev)


def test_survivorship_and_staleness_counted():
    days, bars = _market()
    si = _si()
    del bars["S59"]                                                    # a HIGH-rank name with no bars at all
    bars["S58"] = {k: v[:100] for k, v in bars["S58"].items()}          # bars that stop long before publication
    st = Q.Counter()
    Q.build_events(si, bars, bars, bars["SPY"], Q.PUB_LAG, st)
    assert st["surv_missing_HIGH"] == 1 and st["surv_stale_HIGH"] == 1
    assert Q.bar_status(bars, days, "S58", "2020-02-14") == "stale" and Q.bar_status(bars, days, "S1", days[-1]) == "ok"
