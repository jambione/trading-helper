"""risk_compare: monthly returns, the trend rule, and the risk statistics. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import risk_compare as R  # noqa: E402


def test_month_returns_and_leverage():
    days = ["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28"]
    adj = [100.0, 101.0, 102.01, 103.0301]
    m = R.month_returns(days, adj)
    assert abs(m["2020-01"] - 0.01) < 1e-12 and abs(m["2020-02"] - 0.0201) < 1e-9
    m2 = R.month_returns(days, adj, lev=2.0, fin=0.0)
    assert abs(m2["2020-01"] - 0.02) < 1e-12
    m3 = R.month_returns(days, adj, lev=2.0, fin=0.252)
    assert m3["2020-01"] < m2["2020-01"]


def test_trend_reads_only_closed_months():
    closes = {f"2020-{i:02d}": 100.0 + i for i in range(1, 13)}
    hold = R.trend_in(closes, n=3)
    assert "2020-03" not in hold and hold["2020-04"] is True
    closes["2020-12"] = 1.0          # a crash in December cannot change the decision for December
    assert R.trend_in(closes, n=3)["2020-12"] is True


def test_apply_trend_costs_and_cash():
    rets = {"2020-01": 0.05, "2020-02": -0.10, "2020-03": 0.02}
    out, sw = R.apply_trend(rets, {"2020-01": True, "2020-02": False, "2020-03": True}, 2.0)
    assert out["2020-01"] == 0.05 and abs(out["2020-02"] + 0.0002) < 1e-12 and sw == 2


def test_stats_drawdown_and_recovery():
    s = R.stats([0.10, -0.20, 0.05, 0.25, 0.0], ["2020-01", "2020-02", "2020-03", "2020-04", "2020-05"])
    assert abs(s["mdd"] + 0.20) < 1e-12 and s["longest_under"] == 2 and abs(s["worst_month"] + 0.2) < 1e-12
    assert s["worst_year"] > 0


def test_report_runs_on_synthetic_series():
    import random
    rng = random.Random(0)
    months = [f"{y}-{m:02d}" for y in range(1999, 2027) for m in range(1, 13) if f"{y}-{m:02d}" <= "2026-09"]
    ser = {n: {m: rng.gauss(0.008, 0.04) for m in months} for n in R.NAMES[:4]}
    for n in R.NAMES[4:]:
        ser[n] = {m: rng.gauss(0.01, 0.06) for m in months if m >= "2017-02"}
    ser["_meta"] = {"switches": 10, "cash_months": 20, "hold": {m: True for m in months},
                    "spy_first": "1998-06-01", "spy_last": "2026-10-03"}
    md = R.report(ser)
    assert "W1 common window 2017-02..2026-09" in md and "S5_momentum_trail10" in md and "W2 long window" in md
