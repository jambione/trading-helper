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
