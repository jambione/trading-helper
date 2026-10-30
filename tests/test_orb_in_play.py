"""orb_in_play amended_5: segments and halves, per-day unions and look-back check, SPY hedge, exit minute, verdict."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import orb_in_play as O  # noqa: E402


def test_halves_segments_and_warmup():
    assert O.half_of("2016-03-01") == "H1" and O.half_of("2021-06-01") == "H2"
    assert O.half_of("2016-01-15") is None and O.half_of("2026-02-01") is None and O.half_of("2026-05-20") is None
    assert O.half_of("2026-05-26") == "H2"
    assert O.segment("2026-01-13") == 0 and O.segment("2026-04-27") == 1


def test_unions_cover_the_lookback_and_never_bridge_the_gap():
    days = ["2026-01-12", "2026-01-13", "2026-04-27", "2026-04-28"]
    univ = {"2026-01-12": ["A"], "2026-01-13": ["B"], "2026-04-27": ["C"], "2026-04-28": ["D"]}
    un = O.unions(univ)
    assert un["2026-01-12"] == ["A", "B"] and un["2026-01-13"] == ["B"] and un["2026-04-27"] == ["C", "D"]
    opens = {d: {} for d in days}
    assert O.lookback_misses(univ, opens, un) == {"misses": 0, "missing_sessions": []}
    bad = dict(un, **{"2026-04-27": ["C"]})                     # D's look-back day 04-27 was not requested for D
    assert O.lookback_misses(univ, opens, bad)["misses"] == 1
    assert O.lookback_misses(univ, {d: {} for d in days[:3]}, un)["missing_sessions"] == ["2026-04-28"]


def test_walk_exit_minute_and_spy_hedge():
    rows = [(575, 10.0, 10.1, 9.95, 10.05), (576, 10.05, 10.1, 9.80, 9.9), (959, 9.9, 9.9, 9.9, 9.9)]
    w = O.walk(rows, 0, 10.0, 9.85, 1, True)
    assert w["why"] == "stop" and w["exit_min"] == 576
    spy = {575: (400.0, 400.4), 576: (400.4, 401.0)}
    assert abs(O.spy_ret(spy, 575, 576) - (401.0 / 400.0 - 1)) < 1e-12 and O.spy_ret(spy, 575, 600) is None


def _longs(edge=0.0020, lift=0.0005, years=range(2016, 2027), per_day=8, seed=1):
    rnd = random.Random(seed)
    out = []
    for y in years:
        for mth in range(1, 13):
            for dd in (3, 10, 17, 24):
                day = f"{y}-{mth:02d}-{dd:02d}"
                h = O.half_of(day)
                if h is None:
                    continue
                for i in range(per_day):
                    g = O.COST + edge + rnd.gauss(0, 0.01)
                    c = g - lift + rnd.gauss(0, 0.005)
                    out.append({"day": day, "half": h, "gross": g, "hedged": g, "ctrl": c, "ctrl_hedged": c,
                                "entry": 30.0, "rank": i})
    return out


def test_verdict_labels():
    v = O.verdict(_longs())
    assert v["label"].startswith("PASS") and all(v["checks"].values())
    assert any(t.startswith("SIP-only") for t in v["tags"])
    sel = O.verdict(_longs(lift=-0.0020))
    assert sel["label"].startswith("SELECTION ONLY")
    flat = O.verdict(_longs(edge=-0.0030))
    assert flat["label"] == "FAIL"
    assert O.drop_best_year(_longs(), lambda x: x["gross"] - O.COST)["best_year"] is not None
    assert O.month_t(_longs(), lambda x: x["gross"] - O.COST)["months"] > 100
