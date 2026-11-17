"""market_tape: market-relative admission (ai_relative_strength_admit, default off)."""
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_tape as mt  # noqa: E402

ON = {"ai_relative_strength_admit": True}


def test_off_changes_nothing_and_makes_no_call():
    class Boom:
        def get_stock_snapshot(self, *a, **k):
            raise AssertionError("must not call the API when the flag is off")
    assert mt.effective_min_pct({}, 1.5, -1.0) == 1.5
    assert mt.market_min_pct({}, 1.5, Boom()) == 1.5


def test_red_day_lowers_the_bar_green_day_never_raises_it():
    assert mt.effective_min_pct(ON, 1.5, -1.0) == 0.5
    assert mt.effective_min_pct(ON, 1.5, -4.19) == 1.5 - 4.19
    assert mt.effective_min_pct(ON, 1.5, +0.8) == 1.5
    assert mt.effective_min_pct(ON, 1.5, None) == 1.5          # unknown market: never guess


def _client(px, pc, age_sec):
    ts = datetime.fromtimestamp(1_800_000_000.0 - age_sec, tz=timezone.utc)
    snap = SimpleNamespace(latest_trade=SimpleNamespace(price=px, timestamp=ts),
                           previous_daily_bar=SimpleNamespace(close=pc))

    class C:
        calls = 0

        def get_stock_snapshot(self, req):
            C.calls += 1
            return {"SPY": snap}
    return C()


def test_spy_day_pct_reads_caches_and_refuses_stale(monkeypatch):
    monkeypatch.setattr(mt, "_cache", {"ts": 0.0, "val": None})
    c = _client(594.0, 600.0, 5)
    assert mt.spy_day_pct(c, now=1_800_000_000.0) == -1.0
    assert mt.spy_day_pct(c, now=1_800_000_030.0) == -1.0 and type(c).calls == 1      # cached
    monkeypatch.setattr(mt, "_cache", {"ts": 0.0, "val": None})
    assert mt.spy_day_pct(_client(594.0, 600.0, 900), now=1_800_000_000.0) is None      # 15-min-old print
    monkeypatch.setattr(mt, "_cache", {"ts": 0.0, "val": None})
    assert mt.market_min_pct(ON, 1.5, _client(594.0, 600.0, 5)) == 0.5


def test_tight_screener_uses_the_relative_bar(monkeypatch):
    import tight_screener as ts
    uni = [{"symbol": "UP06", "prior_close": 100.0}, {"symbol": "UP20", "prior_close": 100.0}]
    quotes = {"UP06": (100.6, 1_800_000_000.0), "UP20": (102.0, 1_800_000_000.0)}
    base = ts.movers_select(uni, quotes, now=1_800_000_000.0, min_pct=1.5)
    rel = ts.movers_select(uni, quotes, now=1_800_000_000.0, min_pct=mt.effective_min_pct(ON, 1.5, -1.0))
    assert [r["symbol"] for r in base] == ["UP20"]
    assert sorted(r["symbol"] for r in rel) == ["UP06", "UP20"]


def test_study_rel_bar_matches_the_live_rule():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
    import relative_admit as ra
    for spy in (-4.19, -1.0, 0.0, 0.8):
        assert abs(ra.rel_bar(spy) - mt.effective_min_pct(ON, ra.BASE_PCT, spy)) < 1e-12


def test_study_summary_counts_extra_trades_separately():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
    import relative_admit as ra
    rows = {"d1": [{"spy": -1.0, "base": True, "rel": True, "net": 10.0}, {"spy": -1.0, "base": False, "rel": True, "net": -4.0},
                   {"spy": -1.0, "base": False, "rel": False, "net": 99.0}]}
    s = ra.summarize(rows)["red_days"]
    assert s["base"]["n"] == 1 and s["extra"]["n"] == 1 and s["extra"]["mean_net_bp"] == -4.0
    assert s["extra_minus_base_bp_daymean"] == -14.0
