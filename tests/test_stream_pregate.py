"""No-tape fix (operator 11/04, default off): one-letter tickers may take a stream slot, and the shortlist drops
names with a KNOWN door-refusal reason before asking for slots."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_entry_watch as ew  # noqa: E402

CFG = {"ai_watch_min_price": 20.0, "ai_max_price": 100.0, "ai_watch_max_float_m": 800.0,
       "ai_watch_gap_down_block_pct": 1.0, "ai_watch_max_sip_spread_pct": 0.15,
       "ai_watch_stream_shortlist_max": 0, "ai_watch_allow_one_letter": False}


def _row(sym, px=50.0, src="movers", fl=100.0):
    return {"symbol": sym, "price": px, "source": src, "float_m": fl}


def test_one_letter_tickers_are_off_by_default():
    assert not ew._watch_symbol_ok("T", CFG)
    assert ew._watch_symbol_ok("T", dict(CFG, ai_watch_allow_one_letter=True))
    assert ew._watch_symbol_ok("AB", CFG) and not ew._watch_symbol_ok("ABCDEF", CFG)
    assert not ew._watch_symbol_ok("A1", CFG) and not ew._watch_symbol_ok("", CFG)


def test_pregate_drops_only_on_known_reasons():
    rows = [_row("OKAY"), _row("CHEAP", px=12.0), _row("RICH", px=130.0), _row("BIGF", fl=1500.0),
            _row("NOPX", px=None), _row("NOFL", fl=None)]
    keep, n = ew.stream_pregate(rows, CFG)
    assert keep == ["OKAY", "NOPX", "NOFL"]
    assert n["price"] == 2 and n["float"] == 1 and n["in"] == 6 and n["out"] == 3


def test_pregate_keeps_order_and_caps():
    rows = [_row(s) for s in ("AAA", "BBB", "CCC", "DDD")]
    keep, n = ew.stream_pregate(rows, dict(CFG, ai_watch_stream_shortlist_max=2))
    assert keep == ["AAA", "BBB"] and n["cap"] == 2


def test_pregate_drops_a_cached_gap_down(monkeypatch):
    import time
    monkeypatch.setitem(ew._GAP_CACHE, "GAPD", (-3.0, time.time(), "sip"))
    keep, n = ew.stream_pregate([_row("GAPD"), _row("FINE")], CFG)
    assert keep == ["FINE"] and n["gate"] == 1


def test_pregate_one_letter_follows_the_switch():
    rows = [_row("T", px=24.0), _row("TT")]
    assert ew.stream_pregate(rows, CFG)[0] == ["TT"]
    assert ew.stream_pregate(rows, dict(CFG, ai_watch_allow_one_letter=True))[0] == ["T", "TT"]


def test_ensure_watch_stream_subscribes_one_letter_only_when_on(monkeypatch):
    import types
    got = {}
    fake = types.SimpleNamespace(set_subscribe_priority=lambda x: got.setdefault("pri", list(x)),
                                 get_subscribe_priority=lambda: set(),
                                 request_subscribe=lambda x: got.setdefault("sub", list(x)))
    monkeypatch.setitem(sys.modules, "finnhub_stream", fake)
    monkeypatch.setattr(ew, "push_candidates_to_engine", lambda w: {"pushed": 0})
    ew.ensure_watch_stream(["T", "AAPL"], cfg=CFG)
    assert got["sub"] == ["AAPL"]
    got.clear()
    ew.ensure_watch_stream(["T", "AAPL"], cfg=dict(CFG, ai_watch_allow_one_letter=True))
    assert got["sub"] == ["T", "AAPL"]


def test_switches_default_off_everywhere():
    import json
    import config
    live = json.loads((Path(__file__).resolve().parent.parent / "config" / "bot_config.json").read_text())
    for k in ("ai_watch_allow_one_letter", "ai_watch_stream_pregate"):
        assert config.DEFAULT_CONFIG[k] is False and not live.get(k, False)
    assert config.DEFAULT_CONFIG["ai_watch_stream_shortlist_max"] == 0
