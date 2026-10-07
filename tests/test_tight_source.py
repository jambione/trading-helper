"""The "tight" name source: liquid, tight-spread, moving stocks.

Two halves, as for movers: the producer's selection (tight_screener.py,
pure functions on fake data) and the desk seed reading tight_stocks.json
(ai_entry_watch.desk_candidate_rows). The whole source sits behind
ai_watch_seed_tight, which ships False, so merging it must change nothing.
"""
import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import ai_entry_watch as ew
import tight_screener as ts

ET = ZoneInfo("America/New_York")


# ── producer: universe ──────────────────────────────────────────────────────

def test_universe_keeps_common_stock_only():
    names = {
        "AAPL": "Apple Inc. Common Stock",
        "SPY": "SPDR S&P 500 ETF Trust",          # FUNDISH
        "MIACW": "Some Acquisition Warrant",      # warrant shape
        "TSLL": "Direxion Daily TSLA Bull 2X",    # levered
        "ABCD": "Acme Acquisition Corp",          # SPAC by name
        "BRKB": "Berkshire Hathaway Class B",
        "BRK.B": "Berkshire dotted",              # not alpha
    }
    assert ts.common_symbols(names) == ["AAPL", "BRKB"]


def test_universe_floors_price_and_dollar_volume_and_keeps_top_n():
    prev = {
        "BIG": (150.0, 9e9),
        "MID": (40.0, 2e9),
        "CHEAP": (9.99, 5e9),       # under $10
        "THIN": (80.0, 49e6),       # under $50M
        "EDGE": (10.0, 50e6),       # exactly at both floors: kept
        "SMALL": (20.0, 60e6),
    }
    rows = ts.universe_select(prev, min_price=10.0, min_dollar_volume=50e6, top=3)
    assert [r["symbol"] for r in rows] == ["BIG", "MID", "SMALL"]
    rows = ts.universe_select(prev, min_price=10.0, min_dollar_volume=50e6, top=400)
    assert {r["symbol"] for r in rows} == {"BIG", "MID", "SMALL", "EDGE"}
    assert rows[0]["prior_close"] == 150.0 and rows[0]["prior_dollar_volume"] == 9e9


def test_universe_is_cached_once_per_day(tmp_path):
    calls = {"names": 0, "prev": 0}

    def names():
        calls["names"] += 1
        return {"AAA": "A Corp", "BBB": "B Corp"}

    def prev(syms, day):
        calls["prev"] += 1
        return {"AAA": (20.0, 1e9), "BBB": (30.0, 2e9)}

    p = tmp_path / "u.json"
    cfg = {"ai_tight_min_price": 10, "ai_tight_min_dollar_volume": 50e6,
           "ai_tight_universe_n": 400}
    a = ts.load_universe(cfg, "2026-10-07", fetch_names=names, fetch_prev=prev, path=p)
    b = ts.load_universe(cfg, "2026-10-07", fetch_names=names, fetch_prev=prev, path=p)
    assert [r["symbol"] for r in a] == ["BBB", "AAA"] == [r["symbol"] for r in b]
    assert calls == {"names": 1, "prev": 1}
    assert json.loads(p.read_text())["day"] == "2026-10-07"
    ts._UNIVERSE_MEM.clear()           # a restarted process reads the file
    assert ts.load_universe(cfg, "2026-10-07", fetch_names=names,
                            fetch_prev=prev, path=p) == a
    assert calls == {"names": 1, "prev": 1}
    ts._UNIVERSE_MEM.clear()
    ts._UNIVERSE_TRY.clear()


# ── producer: movers, spread, top-N ─────────────────────────────────────────

UNI = [{"symbol": s, "prior_close": 100.0, "prior_dollar_volume": 1e9}
       for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]


def test_mover_threshold_and_ordering():
    now = 1_000_000.0
    quotes = {
        "AAA": (101.5, now - 5),     # +1.5%
        "BBB": (103.0, now - 5),     # +3.0%
        "CCC": (100.99, now - 5),    # +0.99%: under the floor
        "DDD": (105.0, now - 900),   # +5% but the trade is 15 min old
        "EEE": (101.0, now - 5),     # exactly +1.0%: kept
    }
    got = ts.movers_select(UNI, quotes, now=now, min_pct=1.0)
    assert [r["symbol"] for r in got] == ["BBB", "AAA", "EEE"]
    assert got[0]["pct_change"] == pytest.approx(3.0)


def test_spread_threshold_and_unknown_spread():
    movers = [{"symbol": s, "pct_change": p, "price": 1.0}
              for s, p in (("AAA", 5.0), ("BBB", 4.0), ("CCC", 3.0), ("DDD", 2.0))]
    spreads = {"AAA": 0.05, "BBB": 0.03, "CCC": None, "DDD": 0.01}
    kept, spent = ts.tight_select(movers, spreads.get, max_spread=0.03,
                                  top=15, max_lookups=40)
    assert [r["symbol"] for r in kept] == ["BBB", "DDD"]
    assert kept[0]["spread_pct"] == 0.03
    assert spent == 4


def test_top_n_by_gain_and_the_cap_of_15():
    movers = [{"symbol": f"S{i:02d}", "pct_change": 30.0 - i, "price": 1.0}
              for i in range(25)]
    seen = []

    def spread(sym):
        seen.append(sym)
        return 0.01

    kept, spent = ts.tight_select(movers, spread, max_spread=0.03, top=15,
                                  max_lookups=40)
    assert len(kept) == 15
    assert [r["symbol"] for r in kept] == [f"S{i:02d}" for i in range(15)]
    assert spent == 15 and len(seen) == 15, "no lookups after the list is full"


def test_spread_lookups_are_capped_per_scan():
    movers = [{"symbol": f"S{i:02d}", "pct_change": 30.0 - i, "price": 1.0}
              for i in range(60)]
    kept, spent = ts.tight_select(movers, lambda s: 0.5, max_spread=0.03,
                                  top=15, max_lookups=40)
    assert kept == [] and spent == 40


def test_scan_end_to_end_on_fake_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "SCAN_LEDGER_DIR", tmp_path / "tight_scan")
    t = datetime(2026, 10, 7, 11, 0, tzinfo=ET).timestamp()
    quotes = {"AAA": (102.0, t - 3), "BBB": (101.2, t - 3), "CCC": (104.0, t - 3)}
    spreads = {"AAA": 0.02, "BBB": 0.01, "CCC": 0.09}
    bar = SimpleNamespace(timestamp=datetime(2026, 10, 7, 0, 0, tzinfo=ET),
                          volume=3_000_000, close=102.0)
    old = [SimpleNamespace(timestamp=datetime(2026, 9, 1 + i, tzinfo=ET),
                           volume=4_000_000, close=100.0) for i in range(20)]
    cfg = {"ai_watch_seed_tight": True}
    rows = ts.scan(cfg, now=t, universe=UNI, quotes_fn=lambda syms: quotes,
                   spread_fn=spreads.get, bars_fn=lambda syms: {"AAA": old + [bar]})
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    a = rows[0]
    for k in ("symbol", "price", "pct_change", "spread_pct", "dollar_volume",
              "prior_close"):
        assert k in a
    assert a["source"] == "tight"
    assert a["dollar_volume"] == pytest.approx(3_000_000 * 102.0)
    assert a["rvol_raw"] == pytest.approx(0.75)
    assert rows[1]["dollar_volume"] is None and rows[1]["rvol"] is None, \
        "no bar for today means no measurement, not a guess"
    led = (tmp_path / "tight_scan" / "2026-10-07.jsonl").read_text().splitlines()
    assert json.loads(led[-1])["spread_lookups"] == 3


# ── producer: the switch ────────────────────────────────────────────────────

def test_switch_off_producer_is_a_no_op():
    def boom(*a, **k):
        raise AssertionError("no request may be made while the switch is off")

    assert ts.enabled({}) is False
    assert ts.scan({}, universe=UNI, quotes_fn=boom, spread_fn=boom,
                   bars_fn=boom) is None
    assert ts.scan({"ai_watch_seed_tight": False}, universe=None,
                   quotes_fn=boom, spread_fn=boom, bars_fn=boom) is None


def test_scan_window_waits_for_rth_spread_data():
    cfg = {"ai_movers_sip_delay_min": 15.0}
    assert not ts.in_scan_window(datetime(2026, 10, 7, 9, 45, tzinfo=ET), cfg)
    assert ts.in_scan_window(datetime(2026, 10, 7, 9, 46, tzinfo=ET), cfg)
    assert ts.in_scan_window(datetime(2026, 10, 7, 15, 59, tzinfo=ET), cfg)
    assert not ts.in_scan_window(datetime(2026, 10, 7, 16, 0, tzinfo=ET), cfg)
    assert not ts.in_scan_window(datetime(2026, 10, 10, 11, 0, tzinfo=ET), cfg)  # Sat


def test_producer_launch_is_gated_on_the_switch():
    src = open("trading", encoding="utf-8").read()
    assert "tight_screener.py" in src
    assert "_cfg_flag ai_watch_seed_tight" in src
    assert src.count('pkill -TERM -f "tight_screener.py"') == 1
    assert src.count('pkill -KILL -f "tight_screener.py"') == 1


def test_producer_uses_the_desk_spread_helper():
    src = open(ts.__file__, encoding="utf-8").read()
    assert "ew.sip_spread_pct(" in src


# ── the desk seed ───────────────────────────────────────────────────────────

def _write(tmp_path, monkeypatch, rows, ts_=None):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(ew, "_live_quote_map", lambda: ({}, {}))
    monkeypatch.setattr(ew, "_engine_indicator_map", dict)
    (tmp_path / "tight_stocks.json").write_text(json.dumps({
        "ts": time.time() if ts_ is None else ts_, "rows": rows}), encoding="utf-8")


def _seeded(cfg=None):
    base = {"ai_watch_seed_tight": True, "ai_watch_seed_movers": False,
            "ai_watch_seed_momentum": False, "ai_watch_seed_momentum_open": False,
            "ai_watch_seed_trending": False, "ai_watch_seed_research": False,
            "ai_watch_seed_bb_live": False}
    base.update(cfg or {})
    return [r for r in ew.desk_candidate_rows(base)]


def _row(sym, **kw):
    r = {"symbol": sym, "price": 50.0, "pct_change": 2.0, "spread_pct": 0.02,
         "dollar_volume": 80e6, "prior_close": 49.0, "rvol": 1.5,
         "source": "tight"}
    r.update(kw)
    return r


def test_switch_off_seeds_nothing(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_row("AAA")])
    assert _seeded({"ai_watch_seed_tight": False}) == []
    base = {k: v for k, v in {
        "ai_watch_seed_movers": False, "ai_watch_seed_momentum": False,
        "ai_watch_seed_momentum_open": False, "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False, "ai_watch_seed_bb_live": False}.items()}
    assert ew.desk_candidate_rows(base) == [], "the default is off"


def test_switch_on_seeds_tight_rows(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_row("AAA"), _row("BBB", pct_change=1.2)])
    got = _seeded()
    assert [r["symbol"] for r in got] == ["AAA", "BBB"]
    assert all(r["source"] == "tight" for r in got)


def test_wide_spread_and_cheap_rows_are_refused(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        _row("WIDE", spread_pct=0.08),
        _row("NOSP", spread_pct=None),
        _row("CHEAP", price=9.5),
        _row("FLAT", pct_change=0.4),
        _row("THIN", rvol=0.3),
        _row("SMALL", dollar_volume=1e6),
        _row("OK"),
    ])
    got = _seeded({"ai_watch_movers_min_rvol": 1.0,
                   "ai_watch_hot_move_rvol_waive_pct": 20.0,
                   "ai_watch_movers_min_dollar_volume": 2e6})
    assert [r["symbol"] for r in got] == ["OK"]
    drops = ew.seed_drop_snapshot()["counts"].get("tight", {})
    for why in ("spread_wide", "below_min_price", "pct_low", "thin_rvol",
                "thin_dollar_volume"):
        assert drops.get(why), f"no {why} drop recorded"


def test_seed_count_is_capped(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_row(f"S{c}") for c in "ABCDEFGHIJKL"])
    assert len(_seeded()) == 8, "ai_watch_seed_tight_n defaults to 8"
    assert len(_seeded({"ai_watch_seed_tight_n": 3})) == 3


def test_a_stale_file_seeds_nothing(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_row("AAA")], ts_=time.time() - 4000)
    assert _seeded({"ai_tight_max_age_sec": 900.0}) == []
    _write(tmp_path, monkeypatch, [_row("AAA")], ts_=time.time() - 60)
    assert len(_seeded({"ai_tight_max_age_sec": 900.0})) == 1


def test_a_missing_or_broken_file_seeds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    assert _seeded() == []
    (tmp_path / "tight_stocks.json").write_text("{not json", encoding="utf-8")
    assert _seeded() == []


def test_movers_own_a_name_both_sources_list(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_row("AAA")])
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": time.time(), "rows": [{"symbol": "AAA", "pct_change": 20.0,
                                     "price": 50.0, "rvol": 5.0,
                                     "dollar_volume": 9e7}]}))
    got = _seeded({"ai_watch_seed_movers": True,
                   "ai_watch_movers_min_pct_change": 10.0})
    assert [(r["symbol"], r["source"]) for r in got] == [("AAA", "movers")]


# ── the same gates downstream ───────────────────────────────────────────────

def test_door_spread_cap_applies_to_tight():
    cfg = {"ai_watch_admit_arm_gates": True, "ai_watch_max_sip_spread_pct": 0.05,
           "ai_watch_min_price": 10.0}
    t = datetime(2026, 10, 7, 11, 0, tzinfo=ET).timestamp()
    row = {"symbol": "AAA", "source": "tight", "price": 50.0}
    assert ew.admit_arm_gates(row, cfg, now=t, spread_fn=lambda s, now: 0.08,
                              gap_fn=lambda s, now: None) == (False, "spread_wide")
    assert ew.admit_arm_gates(row, cfg, now=t, spread_fn=lambda s, now: 0.02,
                              gap_fn=lambda s, now: None) == (True, "")
    cheap = dict(row, price=9.0)
    assert ew.admit_arm_gates(cheap, cfg, now=t, spread_fn=lambda s, now: 0.02,
                              gap_fn=lambda s, now: None) == (False, "below_min_price")
    assert ew._spread_gate_max("tight", cfg) == 0.05, "tight is never spread-exempt"


def test_tight_faces_the_movers_admission_floors():
    cfg = {"ai_watch_movers_min_rvol": 1.0, "ai_watch_min_rvol": 2.0,
           "ai_watch_movers_admit_max_tape_age_sec": 60.0}
    assert ew._admit_min_rvol("tight", cfg) == ew._admit_min_rvol("movers", cfg) == 1.0
    assert "tight" in ew._MOVERS_LIKE_SOURCES


# ── registration ────────────────────────────────────────────────────────────

def test_tight_is_registered_in_the_source_sets():
    assert "tight" in ew._ARM_READY_DESK_SOURCES
    assert "tight" in ew._DESK_SOURCES
    assert "tight" in ew._PANEL_SOURCES, "_sync_watch_locked drops unlisted sources"
    assert "tight" in ew._ROSTER_SOURCES
    assert "tight" in ew._PIN_HEAT_SOURCES
    assert "tight" not in ew._RESEARCH_SOURCES
    import book_server
    assert "tight" in book_server.SUPPLY_SOURCES
    import source_norm
    assert source_norm.normalize_proposer("tight") == "tight"


def test_the_knobs_ship_declared_and_off():
    from config import DEFAULT_CONFIG, SAFE_CONFIG_KEYS
    want = {
        "ai_watch_seed_tight": False, "ai_watch_seed_tight_n": 8,
        "ai_tight_scan_sec": 300, "ai_tight_min_pct_change": 1.0,
        "ai_tight_max_spread_pct": 0.03, "ai_tight_min_price": 10,
        "ai_tight_min_dollar_volume": 50e6, "ai_tight_universe_n": 400,
        "ai_tight_top": 15, "ai_tight_max_age_sec": 900,
    }
    for k, v in want.items():
        assert k in DEFAULT_CONFIG, k
        assert DEFAULT_CONFIG[k] == v, k
        assert k in SAFE_CONFIG_KEYS, k
