"""Event eligibility, reaction, markout timing and runway for the news-seat study. No market data."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import news_seat_study as N  # noqa: E402

ET = N.E.ET


def _ts(h, m, s=0):
    return datetime(2026, 10, 8, h, m, s, tzinfo=ET).timestamp()


def _n(sym, h, m, s=0, syms=None):
    return {"id": f"{sym}{h}{m}{s}", "ts": _ts(h, m, s), "symbols": syms or [sym], "headline": "x"}


def test_window_single_symbol_and_freshness():
    news = [
        _n("AAA", 10, 0),
        _n("AAA", 10, 30),                  # 30 min after the last AAA: not fresh
        _n("BBB", 9, 44, 59),               # before 09:45
        _n("CCC", 15, 20),                  # at 15:20: outside [09:45, 15:20)
        _n("DDD", 11, 0, syms=["DDD", "EEE"]),  # two symbols
        _n("BRK.B", 11, 0),                 # not ^[A-Z]{1,5}$
        _n("FFF", 8, 50), _n("FFF", 9, 46),  # premarket article makes 09:46 stale
        _n("GGG", 8, 40), _n("GGG", 9, 50),  # 70 min apart: fresh
    ]
    got = [(e["sym"], e["t0"]) for e in N.eligible_events(news)]
    assert got == [("GGG", 9 * 60 + 50), ("AAA", 10 * 60)]


def test_evaluate_uses_t0_minus_1_and_enters_two_minutes_later():
    t0 = 10 * 60
    sip = {m: (100.0, 100.0) for m in range(t0 - 15, t0 + 40)}
    sip[t0 - 1] = (100.0, 100.0)
    sip[t0 + 1] = (100.3, 100.3)            # reaction bar (e-1)
    sip[t0 + 2] = (100.4, 100.4)            # entry open 100.4
    sip[t0 + 11] = (100.0, 100.9)           # exit close at e+9
    r = N.evaluate({"t0": t0}, sip)
    assert r["entry_min"] == t0 + 2
    assert abs(r["react"] - 0.003) < 1e-12
    assert abs(r["gross"] - (100.9 / 100.4 - 1)) < 1e-12
    del sip[t0 - 1]
    assert N.evaluate({"t0": t0}, sip) is None


def test_runway_hits_and_stops():
    sip = {600 + i: (100.0, 100.0 + 0.1 * i) for i in range(30)}   # climbs 0.1/min
    r = N.runway(sip, 600)
    assert r["up05"] is True and r["up1"] is True
    down = {600: (100.0, 100.0), 601: (100.0, 99.4), 602: (100.0, 101.5)}
    r = N.runway(down, 600)
    assert r["up05"] is False and r["up1"] is False               # -0.6% first


def test_adv_gate_needs_20_prior_sessions():
    rows = [(f"2026-09-{i:02d}", 10.0, 2e6) for i in range(1, 22)]  # $20M/day
    assert N.adv_ok(rows, "2026-09-30") is True
    assert N.adv_ok(rows[:19], "2026-09-30") is None
    assert N.adv_ok([(d, 10.0, 0.5e6) for d, _c, _v in rows], "2026-09-30") is False


def test_half_verdict():
    assert N.half_verdict(0.0001, 2.0, 100, 20) == "PASS"
    assert N.half_verdict(0.0001, 1.9, 100, 20) == "FAIL"
    assert N.half_verdict(-0.0001, 3.0, 100, 20) == "FAIL"
    assert N.half_verdict(0.0001, 2.5, 99, 20) == "UNDERPOWERED"


def test_same_second_duplicates_are_one_event():
    news = [_n("AAA", 10, 0), dict(_n("AAA", 10, 0), id="zzz")]
    assert len(N.eligible_events(news)) == 1


def test_iex_reaction_uses_the_last_print_and_needs_one_after_the_headline():
    t0 = 600
    sip = {m: (100.0, 100.0) for m in range(t0 - 15, t0 + 40)}
    iex = {t0 - 4: (100.0, 100.0), t0 + 1: (100.0, 100.3)}
    r = N.evaluate({"t0": t0}, sip, iex=iex)
    assert abs(r["react_iex"] - 0.003) < 1e-12
    r = N.evaluate({"t0": t0}, sip, iex={t0 - 4: (100.0, 100.0)})
    assert r["react_iex"] is None                 # no IEX print after the headline


def test_twenty_bp_net_must_also_be_positive():
    assert N.half_verdict(0.0005, 2.5, 100, 20, net20=-0.0005) == "FAIL"
    assert N.half_verdict(0.0015, 2.5, 100, 20, net20=0.0005) == "PASS"


def test_latency_logger_rows_and_summary(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import news_latency_logger as LL
    r = LL.row_from({"created_at": "2026-10-08T14:00:00Z", "updated_at": "2026-10-08T14:00:05Z",
                     "id": 7, "symbols": ["AAA"], "headline": "h"}, received=_ts(10, 0, 30))
    assert r["received"] - r["created_at"] == 30.0 and r["symbols"] == ["AAA"]
    monkeypatch.setattr(LL, "OUT_DIR", str(tmp_path))
    with open(tmp_path / "2026-10-14.jsonl", "w") as f:
        for lag in (10, 20, 90):
            f.write(__import__("json").dumps({"created_at": 0.0, "received": float(lag)}) + "\n")
    s = LL.summary("2026-10-14")
    assert "n 3" in s and "<= 60 s 67%" in s
