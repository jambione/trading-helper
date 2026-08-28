"""The Alpaca movers seed, and the two things that would ruin a book with it.

Finnhub has no universe screener, so a price-and-volume seed has to come off
Alpaca's movers ranking. That ranking is raw, and both of its failure modes
were observed on 2026-08-28 before any of this was written:

  Warrants own the top. MIACW +240%, GFAIW +140%, SAIHW +97% — eight of the
  top fifty were warrants at under a dollar. A price band alone does not
  remove them, because some print inside the band.

  RVOL divides by a dormant average. QNRX showed 1281x against a 20-day mean
  of nearly nothing. Arithmetically true, meaningless as a ratio, and it
  would sort straight to the top of any RVOL ranking — selecting exactly the
  thin, gap-through names this desk converts worst.

The filtering that needs a network call lives in the producer; the seed just
reads the file. So these are two suites: symbol/RVOL shape here, and the
seed's own refusals against a written file.
"""
import json
import time

import pytest

import ai_entry_watch as ew
import movers_screener as ms


# ── the producer's refusals ──────────────────────────────────────────────

@pytest.mark.parametrize("sym", ["AAPL", "TSLA", "QNRX", "SWVL", "F", "BRKB"])
def test_common_stock_is_kept(sym):
    assert ms.is_common(sym)


@pytest.mark.parametrize("sym", [
    "MIACW",   # warrant, +240% and $1.02 on the day this was written
    "GFAIW", "SAIHW", "SWVLW", "NCPLW", "ARQQW", "OSPRW",
    "ABCDU",   # unit
    "ABCDR",   # right
    "ABCDQ",   # bankruptcy
])
def test_warrants_units_and_rights_are_refused(sym):
    assert not ms.is_common(sym)


def test_a_four_letter_w_name_is_not_mistaken_for_a_warrant():
    """The suffix rule only applies at five characters. WULF and NEWT are
    ordinary tickers and must not be swept up with the warrants."""
    for sym in ("WULF", "NEWT", "SNOW", "WOW"):
        assert ms.is_common(sym)


def test_junk_symbols_are_refused():
    for sym in ("", None, "BRK.B", "ABCDEF", "123", "A B"):
        assert not ms.is_common(sym)


def test_off_hours_polling_backs_off():
    """Outside 04:00-20:00 ET the movers list is a frozen copy of the last
    session; polling it every minute buys nothing and spends quota."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    assert ms._active_hours(datetime(2026, 8, 28, 9, 30, tzinfo=et))
    assert ms._active_hours(datetime(2026, 8, 28, 4, 0, tzinfo=et))
    assert not ms._active_hours(datetime(2026, 8, 28, 3, 59, tzinfo=et))
    assert not ms._active_hours(datetime(2026, 8, 28, 20, 0, tzinfo=et))
    assert not ms._active_hours(datetime(2026, 8, 29, 10, 0, tzinfo=et))   # Sat


# ── the seed ─────────────────────────────────────────────────────────────

def _write(tmp_path, monkeypatch, rows, ts=None):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "movers_stocks.json").write_text(json.dumps({
        "ts": time.time() if ts is None else ts, "rows": rows}), encoding="utf-8")


def _seeded(cfg=None):
    base = {"ai_watch_seed_movers": True, "ai_watch_seed_momentum": False,
            "ai_watch_seed_momentum_open": False, "ai_watch_seed_trending": False,
            "ai_watch_seed_research": False}
    base.update(cfg or {})
    return [r for r in ew.desk_candidate_rows(base)
            if r.get("source") == "movers"]


def test_a_mover_reaches_the_book(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"symbol": "QNRX", "pct_change": 18.7, "price": 6.12, "rvol": 5.6,
         "source": "movers", "criteria": ["mover"]}])
    got = _seeded()
    assert [r["symbol"] for r in got] == ["QNRX"]
    assert got[0]["source"] == "movers", "the tag is how this source stays measurable"


def test_an_rvol_the_producer_could_not_compute_stays_none(tmp_path, monkeypatch):
    """The QNRX case. A None must travel as None — read as 0 it would fail an
    rvol floor, read as huge it would win an rvol sort. Neither is true."""
    _write(tmp_path, monkeypatch, [
        {"symbol": "QNRX", "pct_change": 18.7, "price": 6.12, "rvol": None,
         "source": "movers", "criteria": ["mover"]}])
    got = _seeded()
    assert len(got) == 1
    assert got[0]["rvol"] is None


def test_a_stale_file_seeds_nothing(tmp_path, monkeypatch):
    """A frozen producer would seed the morning's movers into the afternoon
    book, and every one of them would look like a fresh breakout."""
    _write(tmp_path, monkeypatch, [
        {"symbol": "QNRX", "pct_change": 18.7, "price": 6.12, "rvol": 5.6}],
        ts=time.time() - 4000)
    assert _seeded({"ai_movers_max_age_sec": 900.0}) == []


def test_a_fresh_file_is_not_refused_as_stale(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"symbol": "QNRX", "pct_change": 18.7, "price": 6.12, "rvol": 5.6}],
        ts=time.time() - 60)
    assert len(_seeded({"ai_movers_max_age_sec": 900.0})) == 1


def test_the_day_change_floor_is_enforced_at_the_seed_too(tmp_path, monkeypatch):
    """The producer filters, but the file is on disk and the knob is live —
    a seed that trusted the producer could not be retuned without a restart."""
    _write(tmp_path, monkeypatch, [
        {"symbol": "AAA", "pct_change": 4.0, "price": 6.0, "rvol": 5.0},
        {"symbol": "BBB", "pct_change": 22.0, "price": 6.0, "rvol": 5.0}])
    got = _seeded({"ai_watch_movers_min_pct_change": 10.0})
    assert [r["symbol"] for r in got] == ["BBB"]


def test_a_red_name_never_seeds(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"symbol": "AAA", "pct_change": -12.0, "price": 6.0, "rvol": 9.0}])
    assert _seeded() == []


def test_the_seed_can_be_switched_off(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"symbol": "QNRX", "pct_change": 18.7, "price": 6.12, "rvol": 5.6}])
    assert _seeded({"ai_watch_seed_movers": False}) == []


def test_the_count_is_capped(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        {"symbol": "S%02d" % i, "pct_change": 20.0, "price": 5.0, "rvol": 6.0}
        for i in range(20)])
    assert len(_seeded({"ai_watch_seed_movers_n": 3})) == 3


def test_a_missing_or_broken_file_seeds_nothing_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    assert _seeded() == []
    (tmp_path / "movers_stocks.json").write_text("{not json", encoding="utf-8")
    assert _seeded() == []


def test_the_knobs_ship_declared():
    """A knob absent from DEFAULT_CONFIG reads as None and the gate silently
    does nothing — the failure mode this desk has hit repeatedly."""
    from config import DEFAULT_CONFIG
    for k in ("ai_watch_seed_movers", "ai_watch_seed_movers_n",
              "ai_watch_movers_min_pct_change", "ai_movers_max_age_sec",
              "movers_screener_enabled", "ai_movers_poll",
              "ai_movers_min_avg_vol", "ai_movers_top"):
        assert k in DEFAULT_CONFIG, f"{k} missing from DEFAULT_CONFIG"


def test_the_producer_is_supervised():
    """Started, stopped and reported by the stack script, or it silently
    stops producing and the seed goes quietly empty."""
    src = open("trading", encoding="utf-8").read()
    assert "movers_screener.py" in src
    assert "movers_screener_enabled" in src, "must honour its enable flag"
    assert src.count('pkill -TERM -f "movers_screener.py"') == 1
    assert src.count('pkill -KILL -f "movers_screener.py"') == 1
