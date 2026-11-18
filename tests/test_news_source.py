"""news_source study: causal candidate minutes, comparison and the prereg verdict."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import news_source as ns  # noqa: E402

ET = ZoneInfo("America/New_York")


def ts(h, m, day="2026-11-12"):
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=ET).timestamp()


def test_first_story_minute_is_causal_and_preopen_counts_at_open():
    arts = [{"ts": ts(10, 15), "symbols": ["ABC", "XYZ"]}, {"ts": ts(8, 5), "symbols": ["ABC"]},
            {"ts": ts(16, 30), "symbols": ["LATE"]}, {"ts": ts(11, 0, "2026-11-11"), "symbols": ["OLD"]},
            {"ts": ts(12, 0), "symbols": ["BRK.B", "TOOLONGX"]}]
    m = ns.first_story_minutes(arts, "2026-11-12")
    assert m == {"ABC": 0, "XYZ": 45}            # pre-open story -> 09:30; 16:30 story and other days dropped


def _rows(nets, key=True):
    return [{"base": key, "rel": key, "net": n} for n in nets]


def test_compare_and_verdict():
    days = {f"d{i}": {"base": _rows([5.0, -5.0, 1.0]), "extra": _rows([4.0] * 12)} for i in range(30)}
    c = ns.compare(days)
    assert c["days"] == 30 and c["extra"]["n"] == 360 and c["extra_share_of_base"] == 4.0
    assert ns.verdict(c)[0] == "PASS"
    worse = {f"d{i}": {"base": _rows([5.0, 6.0]), "extra": _rows([-20.0] * 12)} for i in range(30)}
    v, why = ns.verdict(ns.compare(worse))
    assert v == "FAIL" and any("quality_net" in w for w in why)
    few = {f"d{i}": {"base": _rows([1.0]), "extra": _rows([1.0])} for i in range(10)}
    assert ns.verdict(ns.compare(few))[0] == "UNDERPOWERED"


def test_spread_check_gates_both_arms_and_unknown_is_refused():
    sig = {"2026-11-12": {"base": [{"sym": f"B{i}", "t": i, "net": 1.0 if i % 2 else -1.0, "base": True} for i in range(10)],
                          "extra": [{"sym": f"E{i}", "t": i, "net": 2.0, "base": True} for i in range(4)]}}
    plan = ns.check_plan(sig)
    assert sum(1 for p in plan if p[0] == "extra") == 4 and sum(1 for p in plan if p[0] == "base") == 10
    cache = {ns.qkey("2026-11-12", f"B{i}", i): 5.0 for i in range(10)}
    cache[ns.qkey("2026-11-12", "E0", 0)] = 5.0
    cache[ns.qkey("2026-11-12", "E1", 1)] = 40.0          # wide: refused
    cache[ns.qkey("2026-11-12", "E2", 2)] = None          # no quote: refused
    res = ns.score_spreads(plan, cache, 1, 10)            # E3 not fetched: not counted
    assert res["extra"]["measured"] == 3 and res["extra"]["gated"] == 1 and res["extra"]["no_quote"] == 1
    assert res["base_sample"]["pass_rate"] == 1.0
    assert res["extra"]["own_spread_net_bp"] == 2.0 + ns.COST - 5.0
    assert res["verdict"] == "UNDERPOWERED"
