"""Day roster: today's source nominations stay candidates after the list drops them."""
from datetime import datetime
from zoneinfo import ZoneInfo

import ai_entry_watch as ew

ET = ZoneInfo("America/New_York")
ON = {"ai_watch_day_roster": True, "ai_watch_day_roster_max": 40}


def _t(hh, mm, day=24):
    return datetime(2026, 9, day, hh, mm, tzinfo=ET).timestamp()


def _reset(monkeypatch, quotes=None):
    ew._DAY_ROSTER.clear()
    ew._DAY_ROSTER_KEY["day"] = ""
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (quotes or {}, {}))


def test_off_by_default_is_identity(monkeypatch):
    _reset(monkeypatch)
    rows = [{"symbol": "PFE", "source": "movers", "price": 28.6}]
    assert ew.apply_day_roster(rows, {}, now=_t(10, 0)) is rows
    assert ew.apply_day_roster([], {}, now=_t(11, 0)) == []


def test_dropped_name_returns_with_a_current_quote(monkeypatch):
    _reset(monkeypatch, {"CDNA": {"price": 61.9, "pct_change": 14.2}})
    listed = [{"symbol": "CDNA", "source": "movers", "price": 58.0, "pct_change": 9.0,
               "indicator": {"pctr": -80.0}, "criteria": ["movers"]}]
    assert ew.apply_day_roster(listed, ON, now=_t(10, 0)) == listed
    out = ew.apply_day_roster([], ON, now=_t(13, 0))       # list rotated it out
    assert [r["symbol"] for r in out] == ["CDNA"]
    r = out[0]
    assert r["price"] == 61.9 and r["pct_change"] == 14.2  # current, not 10:00's
    assert "indicator" not in r                            # inclusion reads the engine
    assert r["day_roster"] and r["criteria"][-1] == "day_roster"


def test_no_current_quote_means_no_price(monkeypatch):
    """Absence beats a stale number: inclusion then refuses it (no_price)."""
    _reset(monkeypatch)
    ew.apply_day_roster([{"symbol": "DHT", "source": "trending", "price": 21.6, "pct": 1.0}],
                        ON, now=_t(10, 0))
    out = ew.apply_day_roster([], ON, now=_t(13, 0))
    assert out[0]["price"] is None and out[0]["pct"] is None


def test_only_movers_trending_research_join(monkeypatch):
    _reset(monkeypatch)
    ew.apply_day_roster([
        {"symbol": "AAA", "source": "momentum"},
        {"symbol": "BBB", "source": "bb_live"},
        {"symbol": "CCC", "source": "agy", "criteria": ["research"]},
    ], ON, now=_t(10, 0))
    assert [r["symbol"] for r in ew.apply_day_roster([], ON, now=_t(11, 0))] == ["CCC"]


def test_listed_names_are_not_duplicated_and_cap_keeps_the_newest(monkeypatch):
    _reset(monkeypatch)
    for i, s in enumerate(["A", "B", "C"]):
        ew.apply_day_roster([{"symbol": s, "source": "movers"}], ON, now=_t(10, i))
    out = ew.apply_day_roster([{"symbol": "C", "source": "movers"}],
                              {**ON, "ai_watch_day_roster_max": 1}, now=_t(11, 0))
    assert [r["symbol"] for r in out] == ["C", "B"]        # C listed; newest dropped = B


def test_roster_resets_each_day(monkeypatch):
    _reset(monkeypatch)
    ew.apply_day_roster([{"symbol": "A", "source": "movers"}], ON, now=_t(10, 0))
    assert ew.apply_day_roster([], ON, now=_t(10, 0, day=25)) == []


def test_band_filter_frees_slots_only_with_the_roster_on(monkeypatch):
    monkeypatch.setattr(ew, "_live_quote_map", lambda: (
        {"LOW": {"price": 3.2}, "HIGH": {"price": 240.0}, "OK": {"price": 40.0},
         "HELD": {"price": 150.0}}, {}))
    monkeypatch.setattr(ew, "load_watch", lambda: {"HELD": {"status": "filled"}})
    syms = ["LOW", "HIGH", "OK", "HELD", "NOPX"]
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_min_price": 20, "ai_max_price": 100})
    assert ew._push_band_filter(syms) == syms
    monkeypatch.setattr(ew, "_push_cfg", lambda: {**ON, "ai_watch_min_price": 20, "ai_max_price": 100})
    assert ew._push_band_filter(syms) == ["OK", "HELD", "NOPX"]
