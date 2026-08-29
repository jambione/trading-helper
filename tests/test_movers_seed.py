"""The Alpaca movers seed, and the two things that would ruin a book with it.

Finnhub has no universe screener, so a price-and-volume seed has to come off
Alpaca's movers ranking. That ranking is raw, and both of its failure modes
were observed on 2026-08-28 before any of this was written:

  Warrants own the top. MIACW +240%, GFAIW +140%, SAIHW +97% — eight of the
  top fifty were warrants at under a dollar. A price band alone does not
  remove them, because some print inside the band.

  Liquidity has to be measured on today, not on the average. QNRX showed
  1281x, which looked like a divide-by-nothing and is not: SIP says
  31,009,292 shares traded against a 24,203 mean — a dormant shell genuinely
  waking up, and the strongest signal on the list. Flooring the DENOMINATOR
  would have thrown it away; worse, computed on IEX (313-12,565 share
  averages) the floor rejected all eight names. The floor belongs on today's
  dollar volume, which is what "can this be traded" actually asks.

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
              "ai_movers_min_dollar_vol", "ai_movers_top"):
        assert k in DEFAULT_CONFIG, f"{k} missing from DEFAULT_CONFIG"


def test_the_producer_is_supervised():
    """Started, stopped and reported by the stack script, or it silently
    stops producing and the seed goes quietly empty."""
    src = open("trading", encoding="utf-8").read()
    assert "movers_screener.py" in src
    assert "movers_screener_enabled" in src, "must honour its enable flag"
    assert src.count('pkill -TERM -f "movers_screener.py"') == 1
    assert src.count('pkill -KILL -f "movers_screener.py"') == 1


# ── the source tag, which is the whole point ─────────────────────────────

def test_movers_counts_as_desk_heat_not_a_thesis():
    """A seed missing from _DESK_SOURCES does not announce itself.

    _merge_source keeps the SOURCE tag as research (research outranks desk
    heat), but `keep_research` is `prev in _RESEARCH_SOURCES and new in
    _DESK_SOURCES` — so an unregistered seed makes that False and overwrites
    the thesis text and score, leaving a row labelled research wearing
    "mover +41.2%". The tag and the text would disagree.
    """
    assert "movers" in ew._DESK_SOURCES
    assert "movers" not in ew._RESEARCH_SOURCES


def test_a_research_thesis_keeps_the_row_against_a_mover():
    assert ew._merge_source("anthropic", "movers") == "anthropic"
    assert ew._merge_source("xai", "movers") == "xai"


def test_a_mover_still_takes_a_row_from_plain_desk_heat():
    """It is not privileged either — newest desk heat wins, as for trending."""
    assert ew._merge_source("momentum", "movers") == "movers"
    assert ew._merge_source("movers", "trending") == "trending"


def test_every_seed_source_the_loop_can_emit_is_registered():
    """Generalised, so the next new seed cannot repeat this.

    Any source string desk_candidate_rows assigns must be classified as
    research, desk heat, or bb_live — an unclassified one silently loses the
    thesis-ownership argument.
    """
    known = ew._DESK_SOURCES | ew._RESEARCH_SOURCES | ew._BB_LIVE_SOURCES
    for emitted in ("momentum", "trending", "movers", "research"):
        assert emitted in known, f"{emitted} is not classified"


# ── the fields the Scan renderer actually reads ──────────────────────────

def test_a_row_carries_every_field_the_trend_renderer_reads():
    """These rows reuse the Trend row shape, so they must BE that shape.

    feeds.js reads r.trending_score for the Score cell and for the score
    sort, not r.score. Emitting only `score` rendered "—" in Score on every
    movers row and sorted them all as nulls — which looked correct only
    because Alpaca returns the movers already ranked, so the stable sort
    preserved that order by accident.
    """
    import movers_screener as ms
    row = {
        "symbol": "QNRX", "source": "movers", "agreement": True,
        "score": 3.74, "trending_score": 3.74, "reason": "mover +18.7%",
        "pct_change": 18.7, "price": 6.12, "rvol": 5.6, "float_m": 1.9,
        "avg_vol_20d": 24203, "dollar_volume": 189826971, "criteria": ["mover"],
    }
    src = open(ms.__file__, encoding="utf-8").read()
    for field in row:
        assert f'"{field}":' in src, f"producer never emits {field}"


def test_score_and_trending_score_agree():
    """Two names for one number must not drift — the book ranks on one and
    the panel renders the other."""
    import movers_screener as ms
    src = open(ms.__file__, encoding="utf-8").read()
    i = src.index('"score":')
    block = src[i:i + 200]
    assert '"trending_score":' in block, (
        "trending_score must be set next to score, from the same expression")


# ── live enrichment ──────────────────────────────────────────────────────

def _enriched(tmp_path, monkeypatch, file_rows, desk=None, trending=None, cfg=None):
    """Seed movers with a controlled live-quote map."""
    _write(tmp_path, monkeypatch, file_rows)
    monkeypatch.setattr(ew, "_live_quote_map",
                        lambda: (desk or {}, trending or {}))
    return _seeded(cfg)


def test_a_live_desk_quote_beats_the_file(tmp_path, monkeypatch):
    """The whole point. The file says +22% because that is what the producer
    saw; the desk says +11% because that is what the name is worth now."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 5.10, "pct_change": 11.0}})
    assert len(got) == 1
    assert got[0]["pct_change"] == 11.0
    assert got[0]["price"] == 5.10
    assert got[0]["quote_src"] == "desk"


def test_a_faded_name_is_refused_on_its_LIVE_percent(tmp_path, monkeypatch):
    """A timer cannot know this and does not need to. The name qualified when
    the producer wrote it and does not now, so the floor refuses it — which is
    what makes the file's age stop deciding correctness."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 4.90, "pct_change": 1.2}},
        cfg={"ai_watch_movers_min_pct_change": 10.0})
    assert got == []


def test_a_name_that_still_qualifies_survives_a_reranking(tmp_path, monkeypatch):
    """Alpaca caps top at 50 and ranks by percent change, so a name can be
    evicted by hotter movers while still meeting every criterion. Enrichment
    must not punish it for that — it is judged on its number, not its rank."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 6.40, "pct_change": 18.0}})
    assert [r["symbol"] for r in got] == ["AAA"]
    assert got[0]["pct_change"] == 18.0


def test_trending_is_the_fallback_when_the_desk_has_not_seen_it(tmp_path, monkeypatch):
    """Movers names are often new to the book, so the desk map will miss
    them. Trending is the second source before giving up on the file."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        trending={"AAA": {"price": 5.50, "pct_change": 14.0}})
    assert got[0]["pct_change"] == 14.0
    assert got[0]["quote_src"] == "trending"


def test_the_file_is_used_when_no_live_quote_exists(tmp_path, monkeypatch):
    """Degrade to the old behaviour rather than dropping the name. An
    unenrichable row is still a real mover the producer measured."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}])
    assert got[0]["pct_change"] == 22.0
    assert got[0]["quote_src"] == "file"


def test_a_half_populated_live_row_does_not_win(tmp_path, monkeypatch):
    """Price without a percent (or the reverse) would mix one source's price
    with another's change — a row that never existed at any instant."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 5.10}})          # no pct_change
    assert got[0]["quote_src"] == "file"
    assert got[0]["price"] == 6.00


def test_rvol_is_never_enriched(tmp_path, monkeypatch):
    """The guard that matters most. The producer's rvol is SIP on both sides;
    the desk's is IEX. Both render as "x" and they are not the same statistic
    — taking one for the other builds a ratio from two feeds."""
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 6.10, "pct_change": 20.0, "rvol": 99.0}})
    assert got[0]["rvol"] == 5.0, "rvol must stay the producer's SIP reading"


def test_enrichment_can_be_switched_off(tmp_path, monkeypatch):
    got = _enriched(
        tmp_path, monkeypatch,
        [{"symbol": "AAA", "pct_change": 22.0, "price": 6.00, "rvol": 5.0}],
        desk={"AAA": {"price": 5.10, "pct_change": 11.0}},
        cfg={"ai_watch_movers_enrich": False})
    assert got[0]["pct_change"] == 22.0
    assert got[0]["quote_src"] == "file"


def test_both_seeds_share_one_quote_map():
    """Two copies would drift into two ideas of "the live price"."""
    src = open("ai_entry_watch.py", encoding="utf-8").read()
    assert src.count("def _live_quote_map(") == 1
    assert src.count("_live_quote_map()") >= 2


# ── tape continuity ──────────────────────────────────────────────────────

def test_the_continuity_filter_measures_coverage_not_a_sum():
    """A daily dollar-volume floor cannot see a hole in the tape.

    Measured on 2026-08-28's own picks, all of which cleared the $1M sum:

        QNRX  100% of minutes traded   0m longest gap
        SWVL   99%                     1m
        IREZ   93%                     2m
        JELD   88%                     4m
        TJGC   53%                     9m
        RDIB   42%                    54m      $12.4M total
        YDES   29%                    31m      $706K total, $1,920/min median
        AKTX   28%                    35m

    Half of them were untradeable and the producer could not tell. It matters
    because the working shelf sits 0.25% under the fill: across a 30-minute
    hole the next print can be several tenths of a percent away, so the stop
    is set by whoever crosses next rather than by the move.

    RDIB is why the test is coverage and not median-per-minute — its median
    traded minute was a healthy $36k. It simply did not trade in most of them.
    """
    src = open("movers_screener.py", encoding="utf-8").read()
    assert "ai_movers_min_live_pct" in src
    assert "TimeFrameUnit.Minute" in src, "coverage needs minute bars"
    assert "live_pct[sym] = live / float(max(1, open_min))" in src


def test_an_unmeasured_name_is_not_refused():
    """No reading is no opinion. Refusing every name because one bar request
    failed would empty the book on an API hiccup — this filter is about tape
    quality, not availability."""
    src = open("movers_screener.py", encoding="utf-8").read()
    assert "lp = live_pct.get(sym)" in src
    assert "if min_live_pct > 0 and lp is not None and lp < min_live_pct:" in src, (
        "a missing reading must not count as a failing one")
    assert "live_pct = {}" in src, "a failed fetch disables the filter, not the pass"


def test_the_window_is_trailing_not_the_whole_session():
    """Cheaper, reflects liquidity NOW, and lets a name that just woke up
    qualify intraday rather than waiting for tomorrow."""
    src = open("movers_screener.py", encoding="utf-8").read()
    assert "timedelta(minutes=live_win)" in src


def test_live_pct_travels_on_the_row():
    """So a thin admission can be explained after the fact instead of
    argued about."""
    src = open("movers_screener.py", encoding="utf-8").read()
    assert '"live_pct":' in src


def test_the_continuity_knobs_ship_declared():
    from config import DEFAULT_CONFIG
    for k in ("ai_movers_min_live_pct", "ai_movers_live_window_min",
              "ai_movers_min_minute_dollars"):
        assert k in DEFAULT_CONFIG, f"{k} missing from DEFAULT_CONFIG"
    assert DEFAULT_CONFIG["ai_movers_min_live_pct"] > 0, (
        "shipped on — the 2026-08-28 list was half untradeable without it")


def test_the_window_counts_only_open_minutes():
    """A minute the market was shut is not a minute a name failed to trade in.

    Dividing by wall-clock minutes made every name read 0% outside RTH. It
    emptied the book the first time it ran, on a Saturday, and it would have
    done the same every premarket — the producer starts at 04:00, when a
    trailing hour covers 03:00-04:00 and nothing trades in it for ANY symbol.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import movers_screener as ms
    et = ZoneInfo("America/New_York")
    # Mid-session: the whole window is open.
    assert ms._rth_minutes_in_window(60, datetime(2026, 8, 31, 14, 0, tzinfo=et)) == 60
    # Half an hour after the open: only 30 of the 60 were.
    assert ms._rth_minutes_in_window(60, datetime(2026, 8, 31, 10, 0, tzinfo=et)) == 30
    # Premarket and weekends have none.
    assert ms._rth_minutes_in_window(60, datetime(2026, 8, 31, 4, 30, tzinfo=et)) == 0
    assert ms._rth_minutes_in_window(60, datetime(2026, 8, 29, 14, 0, tzinfo=et)) == 0


def test_too_little_tape_forms_no_opinion():
    """At the open there is not yet an hour of session to judge against, and
    'not enough evidence' must not read as 'failed'."""
    src = open("movers_screener.py", encoding="utf-8").read()
    assert "open_min >= need_min" in src
    assert "ai_movers_live_min_open_minutes" in src
    assert "live / float(max(1, open_min))" in src, (
        "the denominator must be open minutes, not the wall-clock window")
