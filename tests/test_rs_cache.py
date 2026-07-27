"""
test_rs_cache.py — the SQLite bar store and the split detector, offline.

No network: every test builds its own database under tmp_path and hands it
frames it constructed. The split-detection arithmetic (split_ratio,
needs_repair) is pure and gets exercised with plain dicts.

The property most of this file exists to pin: a corporate action restates the
vendor's whole history, so a cache holding pre-split prices is silently
inconsistent with post-split ones, and on a 252-session lookback that becomes a
fake ±50-90% return on a name that did nothing.

Run:
    .venv/bin/python -m pytest tests/test_rs_cache.py -q
"""

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rs_cache as rcache   # noqa: E402


def bars(closes, start="2026-01-05", volume=1_000_000.0) -> pd.DataFrame:
    """Oldest→newest daily OHLCV stamped the way Alpaca stamps daily bars —
    05:00 UTC, i.e. midnight-ish ET."""
    idx = pd.bdate_range(start, periods=len(closes), tz="UTC") + pd.Timedelta(hours=5)
    return pd.DataFrame({
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": [volume] * len(closes),
    }, index=idx)


@pytest.fixture()
def cache(tmp_path):
    with rcache.BarCache(tmp_path / "rs.sqlite") as c:
        yield c


# ── The split detector (pure) ─────────────────────────────────────────────────

def test_a_two_for_one_split_halves_every_overlapping_close():
    cached = {"2026-03-02": 100.0, "2026-03-03": 102.0, "2026-03-04": 104.0}
    fetched = {s: v / 2 for s, v in cached.items()}
    assert rcache.needs_repair(cached, fetched) is True
    assert rcache.split_ratio(cached, fetched) == pytest.approx(0.5)


def test_a_reverse_split_multiplies_every_overlapping_close():
    cached = {"2026-03-02": 0.40, "2026-03-03": 0.42}
    fetched = {s: v * 10 for s, v in cached.items()}
    assert rcache.needs_repair(cached, fetched) is True
    assert rcache.split_ratio(cached, fetched) == pytest.approx(10.0)


def test_a_half_cent_bar_correction_is_not_mistaken_for_a_split():
    """Alpaca revises a bar by a fraction of a cent for late prints. The
    smallest common split is 3:2 (+50%), so 0.5% sits well between them."""
    cached = {"2026-03-02": 100.0, "2026-03-03": 102.0}
    fetched = {"2026-03-02": 100.05, "2026-03-03": 102.0}
    assert rcache.needs_repair(cached, fetched) is False


def test_a_symbol_with_no_overlapping_sessions_yields_no_ratio_rather_than_a_default():
    assert rcache.split_ratio({"2026-03-02": 10.0}, {"2026-04-02": 20.0}) is None
    assert rcache.needs_repair({"2026-03-02": 10.0}, {"2026-04-02": 20.0}) is False


def test_the_median_ignores_one_bad_bar_in_the_overlap():
    cached = {"a": 100.0, "b": 100.0, "c": 100.0}
    fetched = {"a": 50.0, "b": 50.0, "c": 999.0}
    assert rcache.split_ratio(cached, fetched) == pytest.approx(0.5)


# ── Storage ───────────────────────────────────────────────────────────────────

def test_a_reopened_cache_returns_what_the_last_run_wrote(tmp_path):
    path = tmp_path / "rs.sqlite"
    with rcache.BarCache(path) as c:
        c.upsert("AAA", bars([10.0, 11.0, 12.0]))
    with rcache.BarCache(path) as c:
        frame = c.get("AAA")
        assert list(frame["close"]) == [10.0, 11.0, 12.0]
        assert isinstance(frame.index, pd.DatetimeIndex)


def test_reinserting_the_same_session_updates_rather_than_duplicates(cache):
    cache.upsert("AAA", bars([10.0, 11.0, 12.0]))
    cache.upsert("AAA", bars([10.0, 11.0, 99.0]))
    frame = cache.get("AAA")
    assert len(frame) == 3
    assert frame["close"].iloc[-1] == 99.0


def test_a_session_newer_than_the_cutoff_is_not_stored(cache):
    """The in-progress session must never enter the cache. Alpaca serves a
    partial bar for a live session; caching it means the next refresh compares
    that partial close against a later partial close of the SAME day, exceeds
    the split tolerance, and 'repairs' a symbol that never split.

    Observed live before this guard: 78 spurious repairs across a 1,500-name
    universe from two runs twenty minutes apart.
    """
    written = cache.upsert("AAA", bars([10.0, 11.0, 12.0], start="2026-07-22"),
                           max_session="2026-07-23")
    assert written == 2
    assert set(cache.closes("AAA")) == {"2026-07-22", "2026-07-23"}
    assert cache.last_session("AAA") == "2026-07-23"


def test_without_a_cutoff_every_session_is_stored(cache):
    assert cache.upsert("AAA", bars([10.0, 11.0, 12.0], start="2026-07-22")) == 3


def test_a_bar_with_no_close_is_not_stored(cache):
    frame = bars([10.0, 11.0, 12.0])
    frame.loc[frame.index[1], "close"] = float("nan")
    assert cache.upsert("AAA", frame) == 2
    assert len(cache.get("AAA")) == 2


def test_bars_are_keyed_by_eastern_session_date_not_utc_timestamp(cache):
    """Alpaca stamps daily bars at 04:00 UTC in EDT and 05:00 UTC in EST. Keying
    on the UTC date would file every EDT bar under the following day."""
    edt = pd.DataFrame({"close": [10.0]},
                       index=[datetime(2026, 7, 3, 4, 0, tzinfo=timezone.utc)])
    est = pd.DataFrame({"close": [11.0]},
                       index=[datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)])
    cache.upsert("AAA", edt)
    cache.upsert("AAA", est)
    assert set(cache.closes("AAA")) == {"2026-07-03", "2026-01-05"}


def test_the_index_survives_a_round_trip_so_anchors_can_be_aligned(cache):
    """swing_screener._clean_bars drops the index; this store must not, or a
    gappy symbol's anchors silently shift against the benchmark."""
    cache.upsert("AAA", bars([10.0, 11.0, 12.0]))
    frame = cache.get("AAA")
    assert frame.index.tz is not None
    assert frame.index.is_monotonic_increasing


def test_a_session_read_back_out_is_the_same_calendar_day_it_went_in(cache):
    """The round trip has to preserve the ET session, not just the string.

    Rebuilt at midnight UTC, an ET session of 2026-07-24 converts back to
    2026-07-23 in summer — which would make `as_of` name the wrong tape and
    shift every anchor by one session.
    """
    summer = pd.DataFrame({"close": [10.0]},
                          index=[datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)])
    winter = pd.DataFrame({"close": [11.0]},
                          index=[datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)])
    cache.upsert("AAA", summer)
    cache.upsert("AAA", winter)

    frame = cache.get("AAA")
    dates = [ts.tz_convert("America/New_York").date().isoformat() for ts in frame.index]
    assert dates == ["2026-01-05", "2026-07-24"]
    assert dates == sorted(cache.closes("AAA"))


def test_get_many_anchors_sessions_the_same_way_get_does(cache):
    cache.upsert("AAA", bars([10.0, 11.0], start="2026-07-22"))
    single = cache.get("AAA").index
    batched = cache.get_many(["AAA"])["AAA"].index
    assert list(single) == list(batched)


def test_get_many_returns_only_the_symbols_asked_for(cache):
    cache.upsert("AAA", bars([1.0, 2.0]))
    cache.upsert("BBB", bars([3.0, 4.0]))
    cache.upsert("CCC", bars([5.0, 6.0]))
    out = cache.get_many(["AAA", "CCC"])
    assert set(out) == {"AAA", "CCC"}
    assert list(out["CCC"]["close"]) == [5.0, 6.0]


# ── Refresh buckets ───────────────────────────────────────────────────────────

def test_a_symbol_never_seen_before_is_not_in_the_incremental_bucket(cache):
    """New listings must land in the backfill bucket or they never get history."""
    cache.upsert("AAA", bars([1.0, 2.0]))
    known = cache.known_symbols()
    assert "AAA" in known
    assert "NEWIPO" not in known


def test_the_last_session_is_what_drives_the_incremental_start(cache):
    cache.upsert("AAA", bars([1.0, 2.0, 3.0], start="2026-01-05"))
    assert cache.last_session("AAA") == "2026-01-07"
    assert cache.last_sessions()["AAA"] == "2026-01-07"


def test_last_sessions_answers_for_the_whole_universe_in_one_query(cache):
    for sym in ("AAA", "BBB", "CCC"):
        cache.upsert(sym, bars([1.0, 2.0]))
    assert set(cache.last_sessions()) == {"AAA", "BBB", "CCC"}


def test_a_symbol_the_vendor_has_no_bars_for_is_not_re_requested_all_day(cache):
    """~1% of the tradable list (ADRs, thin foreign listings) has no IEX history
    at all. Unmarked they look like new listings on every run and the full
    backfill window is re-requested forever — the bars never arrive, only the
    request bill does."""
    cache.mark_empty("HKXCY")
    assert "HKXCY" in cache.recently_empty()
    assert cache.last_session("HKXCY") is None


def test_the_empty_marker_expires_so_a_real_new_listing_is_picked_up(cache):
    cache.mark_empty("NEWCO")
    assert "NEWCO" not in cache.recently_empty(within_sec=0)


def test_a_symbol_with_bars_is_never_treated_as_empty(cache):
    cache.upsert("AAA", bars([1.0, 2.0]))
    assert "AAA" not in cache.recently_empty()


def test_bars_arriving_later_clear_the_empty_marker(cache):
    cache.mark_empty("LATE")
    cache.upsert("LATE", bars([1.0, 2.0]))
    assert "LATE" not in cache.recently_empty()
    assert cache.last_session("LATE") is not None


# ── Repair ────────────────────────────────────────────────────────────────────

def test_a_split_after_caching_is_detected_and_the_symbol_is_refulled(cache):
    """The full cycle: seed history, the vendor halves it, purge and refetch."""
    cache.upsert("AAA", bars([100.0] * 60, start="2026-01-05"))
    cached = cache.closes("AAA")

    refetched = bars([50.0] * 5, start="2026-03-20")     # overlapping, restated
    fetched = {rcache._session_key(ts): float(v)
               for ts, v in refetched["close"].items()}
    overlap = {s: v for s, v in cached.items() if s in fetched}
    assert overlap, "the overlap window must actually overlap"

    assert rcache.needs_repair(overlap, fetched) is True
    ratio = rcache.split_ratio(overlap, fetched)
    cache.purge("AAA")
    cache.record_repair("AAA", ratio)
    cache.upsert("AAA", bars([50.0] * 60, start="2026-01-05"))

    assert cache.symbol_meta("AAA")["repairs"] == 1
    assert cache.symbol_meta("AAA")["last_repair_ratio"] == pytest.approx(0.5)
    assert max(cache.closes("AAA").values()) == 50.0    # no pre-split price survived


def test_purging_leaves_no_bars_but_keeps_the_repair_history(cache):
    cache.upsert("AAA", bars([10.0, 11.0]))
    cache.record_repair("AAA", 0.5)
    cache.purge("AAA")
    assert cache.closes("AAA") == {}
    assert cache.symbol_meta("AAA")["repairs"] == 1
    assert cache.last_session("AAA") is None


def test_repairs_accumulate_across_runs(cache):
    cache.upsert("AAA", bars([10.0]))
    cache.record_repair("AAA", 0.5)
    cache.record_repair("AAA", 2.0)
    meta = cache.symbol_meta("AAA")
    assert meta["repairs"] == 2
    assert meta["last_repair_ratio"] == pytest.approx(2.0)


# ── Adjustment provenance ─────────────────────────────────────────────────────

def test_a_cache_holding_split_bars_refuses_to_serve_a_run_configured_for_all(tmp_path):
    """Mixing adjustment bases compares prices measured two different ways. An
    error beats a silent 10-minute refetch triggered by a config typo."""
    path = tmp_path / "rs.sqlite"
    with rcache.BarCache(path, adjustment="split") as c:
        c.upsert("AAA", bars([10.0]))
    with pytest.raises(rcache.AdjustmentMismatch, match="rebuild|Rebuild"):
        rcache.BarCache(path, adjustment="all")


def test_the_cache_records_which_adjustment_and_feed_its_bars_are(tmp_path):
    with rcache.BarCache(tmp_path / "rs.sqlite", adjustment="split", feed="iex") as c:
        c.upsert("AAA", bars([10.0]))
        assert c.get_meta("adjustment") == "split"
        assert c.symbol_meta("AAA")["adjustment"] == "split"
        assert c.symbol_meta("AAA")["feed"] == "iex"


def test_reopening_with_the_same_adjustment_is_fine(tmp_path):
    path = tmp_path / "rs.sqlite"
    with rcache.BarCache(path, adjustment="split") as c:
        c.upsert("AAA", bars([10.0]))
    with rcache.BarCache(path, adjustment="split") as c:
        assert len(c.get("AAA")) == 1


# ── Trim ──────────────────────────────────────────────────────────────────────

def test_the_trim_keeps_the_database_flat(cache):
    cache.upsert("AAA", bars([float(i) for i in range(100)], start="2025-06-02"))
    before = len(cache.closes("AAA"))
    removed = cache.trim("2025-09-01")
    assert removed > 0
    assert len(cache.closes("AAA")) == before - removed
    assert min(cache.closes("AAA")) >= "2025-09-01"


# ── Ratings table ─────────────────────────────────────────────────────────────

def test_the_ratings_table_answers_for_a_symbol_the_filters_dropped(cache):
    """The percentile is only auditable if the unranked-but-rated names are
    recorded — they never appear in the served candidate list."""
    rows = [{"ticker": "KEPT", "rs_rating": 95, "rs_raw": 2.0},
            {"ticker": "DROPPED", "rs_rating": 91, "rs_raw": 1.9}]
    assert cache.write_ratings("2026-07-24", rows, "trailing", 8143) == 2

    history = cache.rating_history("DROPPED")
    assert history[0]["rs_rating"] == 91
    assert history[0]["population"] == 8143
    assert history[0]["rs_form"] == "trailing"


def test_rerunning_the_same_session_updates_the_rating_rather_than_duplicating(cache):
    cache.write_ratings("2026-07-24", [{"ticker": "AAA", "rs_rating": 50, "rs_raw": 1.0}],
                        "trailing", 100)
    cache.write_ratings("2026-07-24", [{"ticker": "AAA", "rs_rating": 91, "rs_raw": 2.0}],
                        "trailing", 100)
    history = cache.rating_history("AAA")
    assert len(history) == 1
    assert history[0]["rs_rating"] == 91


def test_rating_history_comes_back_newest_first(cache):
    for day, rating in (("2026-07-22", 80), ("2026-07-23", 85), ("2026-07-24", 90)):
        cache.write_ratings(day, [{"ticker": "AAA", "rs_rating": rating, "rs_raw": 1.0}],
                            "trailing", 100)
    assert [h["as_of"] for h in cache.rating_history("AAA")] == \
        ["2026-07-24", "2026-07-23", "2026-07-22"]


def test_an_unrated_symbol_is_recorded_as_null_not_zero(cache):
    cache.write_ratings("2026-07-24", [{"ticker": "NOHIST", "rs_rating": None, "rs_raw": None}],
                        "trailing", 100)
    assert cache.rating_history("NOHIST")[0]["rs_rating"] is None


def test_stats_reports_what_the_cache_holds(cache):
    cache.upsert("AAA", bars([1.0, 2.0, 3.0]))
    cache.upsert("BBB", bars([4.0, 5.0]))
    stats = cache.stats()
    assert stats["symbols"] == 2
    assert stats["bars"] == 5
