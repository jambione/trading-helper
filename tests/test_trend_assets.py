"""tools/studies/trend_assets.py: rules read only closes up to the decision; turnover and drawdown maths."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import trend_assets as T


def test_rules_use_only_past_closes():
    up = [100 + i for i in range(30)]
    down = [130 - i for i in range(30)]
    cl = {f: list(up) for f in T.FUNDS}
    cl["DBC"] = list(down)
    w = T.rule_faber(cl, 20)
    assert abs(w["BIL"] - 0.2) < 1e-12 and abs(w["SPY"] - 0.2) < 1e-12 and "DBC" not in w
    cl2 = {k: v[:21] + [1.0] * 9 for k, v in cl.items()}               # garbage after the decision month
    assert T.rule_faber(cl2, 20) == w
    cl["SPY"], cl["EFA"], cl["BIL"] = up, [100 + 2 * i for i in range(30)], [100.0] * 30
    assert T.rule_dual(cl, 20) == {"EFA": 1.0}
    cl["SPY"], cl["EFA"] = down, down
    assert T.rule_dual(cl, 20) == {"AGG": 1.0}


def test_simulate_costs_and_stats():
    rets = [{f: 0.01 for f in T.FUNDS} for _ in range(30)]
    r = T.simulate(T.fixed({"SPY": 1.0}), {}, rets, 12)
    assert abs(r[0] - (0.01 - T.COST)) < 1e-12 and all(abs(x - 0.01) < 1e-12 for x in r[1:])     # cost only on the first buy
    s = T.stats([0.1, -0.5, 0.2], [0, 0, 0])
    assert abs(s["dd"] + 0.5) < 1e-12 and s["months"] == 3
    assert T.month_ends(["2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02"]) == [0, 2]
