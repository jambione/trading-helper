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
    assert Q.is_etf_like("Realty Income Trust")            # REIT 'Trust' is removed too (declared)


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


def test_nw_and_clustered_se():
    xs = [1.0, -1.0] * 20
    assert Q.nw_se(xs) is not None and Q.clustered_se([1.0, 2.0], ["a", "a"]) is None
