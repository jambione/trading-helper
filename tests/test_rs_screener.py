"""
test_rs_screener.py — the RS fetcher and orchestrator, offline.

No network: Alpaca is monkeypatched at rs_fetch.fetch_daily_bars, which is the
seam rs_screener.refresh_cache calls. The request builder is exercised directly
so the two SDK traps it codes around can be asserted on without a round trip.

The two most important tests in the suite are
test_the_bars_request_asks_for_split_adjustment and
test_no_limit_is_set_so_the_sdk_pages_past_ten_thousand_bars: both guard defects
that produce plausible-looking wrong output rather than an error.

Run:
    .venv/bin/python -m pytest tests/test_rs_screener.py -q
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rs_cache      # noqa: E402
import rs_core       # noqa: E402
import rs_fetch      # noqa: E402
import rs_screener   # noqa: E402

ET = ZoneInfo("America/New_York")

SESSIONS = 260
LAST_SESSION = "2026-07-24"
# A Monday morning after the fixture's last session, before the 18:00 settle.
NOW_ET = datetime(2026, 7, 27, 9, 0, tzinfo=ET)


def calendar(n: int = SESSIONS) -> pd.DatetimeIndex:
    return (pd.bdate_range(end=LAST_SESSION, periods=n, tz="UTC")
            + pd.Timedelta(hours=5))


def ramp(start: float, end: float, n: int = SESSIONS, volume: float = 5_000_000.0):
    idx = calendar(n)
    closes = pd.Series([start + (end - start) * i / (n - 1) for i in range(n)], index=idx)
    return pd.DataFrame({
        "close": closes,
        "high": closes * 1.03,
        "low": closes * 0.97,
        "volume": volume,
    }, index=idx)


def universe_bars(count: int = 60) -> dict[str, pd.DataFrame]:
    """`count` names of increasing strength, plus a flat SPY."""
    data = {"SPY": ramp(400.0, 500.0)}
    for i in range(count):
        # S00 falls, S59 quadruples — a clean strength ordering to rank.
        data[f"S{i:02d}"] = ramp(100.0, 60.0 + 6.0 * i)
    return data


def cfg_for(tmp_path, **overrides) -> dict:
    cfg = {
        "rs_cache_path": str(tmp_path / "rs.sqlite"),
        "rs_bar_adjustment": "split",
        "rs_lookback_sessions": 252,
        "rs_backfill_calendar_days": 400,
        "rs_overlap_sessions": 5,
        "rs_min_coverage": 0.80,
        "rs_min_population": 10,
        "rs_max_stale_frac": 0.10,
        "rs_max_p0_staleness_sessions": 1,
        "rs_form": "trailing",
        "rs_benchmark": "SPY",
        "rs_exclude_etp": False,
        "rs_limit": 100,
        "rs_min_rs_rating": 0,
        "rs_min_price": 0.0,
        "rs_min_avg_vol_50d": 0.0,
        "rs_require_above_sma50": False,
        "rs_require_above_sma200": False,
        "api_key": "k", "secret_key": "s",
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Patch the network seam, the universe and the output path."""
    data = universe_bars()

    def fake_fetch(symbols, start, cfg, budget=None, adjustment="split",
                   chunk=100, client=None, on_progress=None):
        return {s: data[s].copy() for s in symbols if s in data}, set()

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")
    return data


# ── The request: the two SDK traps ────────────────────────────────────────────

def test_the_bars_request_asks_for_split_adjustment():
    """Alpaca's default is RAW. Measured on live data, 2.3% of symbols carry a
    >1.8x single-day raw gap that split adjustment removes — and those names land
    at the extremes of the percentile, i.e. the top of the output."""
    from alpaca.data.enums import Adjustment
    req = rs_fetch.build_bars_request(["AAPL"], datetime(2025, 1, 1), {})
    assert req.adjustment == Adjustment.SPLIT


def test_no_limit_is_set_so_the_sdk_pages_past_ten_thousand_bars():
    """alpaca/common/rest.py:383-390 only paginates while `limit` is unset.
    Setting limit=10000 caps the WHOLE batch at 10,000 bars — the first ~40 of
    100 symbols — silently."""
    req = rs_fetch.build_bars_request([f"S{i}" for i in range(100)],
                                      datetime(2025, 1, 1), {})
    assert req.limit is None


def test_raw_adjustment_is_refused_rather_than_quietly_accepted():
    """RAW is also the API default, so a silent fallback would be
    indistinguishable from forgetting the flag entirely."""
    with pytest.raises(ValueError, match="refusing adjustment"):
        rs_fetch.build_bars_request(["AAPL"], datetime(2025, 1, 1), {}, adjustment="raw")


def test_the_request_asks_for_daily_bars_off_the_iex_feed():
    from alpaca.data.enums import DataFeed
    req = rs_fetch.build_bars_request(["AAPL"], datetime(2025, 1, 1), {})
    assert req.feed == DataFeed.IEX
    assert req.timeframe.value == "1Day"


# ── Page accounting and the throttle ──────────────────────────────────────────

def test_pages_are_counted_from_the_bars_returned():
    assert rs_fetch.pages_used(0) == 1          # an empty response still cost a trip
    assert rs_fetch.pages_used(9_999) == 1
    assert rs_fetch.pages_used(10_000) == 1
    assert rs_fetch.pages_used(10_001) == 2
    assert rs_fetch.pages_used(27_500) == 3


def test_the_throttle_paces_the_run_under_the_configured_ceiling():
    """Measured at ~4 pages/sec unthrottled, which sustains ~240/min against a
    200/min ceiling. The budget must actually block."""
    now = [0.0]
    slept: list[float] = []

    budget = rs_fetch.RateBudget(max_per_min=10, clock=lambda: now[0],
                                 sleep=lambda s: slept.append(s))
    for _ in range(10):
        assert budget.wait() == 0.0
        budget.charge(1)

    assert budget.wait() > 0            # the 11th call inside the window waits
    assert slept and slept[0] == pytest.approx(60.0)


def test_the_budget_charges_every_page_not_every_call():
    budget = rs_fetch.RateBudget(max_per_min=100, clock=lambda: 0.0, sleep=lambda s: None)
    budget.charge(rs_fetch.pages_used(27_500))
    assert budget.pages == 3


def test_the_window_rolls_so_an_idle_minute_frees_the_budget():
    now = [0.0]
    budget = rs_fetch.RateBudget(max_per_min=2, clock=lambda: now[0], sleep=lambda s: None)
    budget.charge(2)
    now[0] = 61.0
    assert budget.wait() == 0.0


def test_a_backfill_chunk_is_smaller_than_an_incremental_one():
    """The page cap counts bars, not symbols."""
    assert rs_fetch.chunk_size_for(275) < rs_fetch.chunk_size_for(6)
    assert rs_fetch.chunk_size_for(6) == 100          # capped by the symbol limit


def test_the_incremental_path_may_batch_more_symbols_than_a_backfill():
    """Six sessions x 300 symbols is 1,800 bars — still one page. This is the
    only place chunk size genuinely cuts the request count."""
    assert rs_fetch.chunk_size_for(15, 300) == 300
    assert rs_fetch.chunk_size_for(15, 300) * 15 < rs_fetch.PAGE_CAP


# ── Response handling ─────────────────────────────────────────────────────────

def test_the_daily_frame_keeps_its_timestamps():
    """swing_screener._clean_bars:189 ends in reset_index(drop=True). Without
    dates a gappy symbol cannot be aligned and every anchor silently shifts."""
    frame = ramp(10.0, 20.0, n=5)
    frame.index = pd.MultiIndex.from_product([["AAA"], frame.index],
                                             names=["symbol", "timestamp"])
    out = rs_fetch._split_response(frame, ["AAA"])
    assert isinstance(out["AAA"].index, pd.DatetimeIndex)
    assert out["AAA"].index.is_monotonic_increasing


def test_a_failed_chunk_reports_its_symbols_as_unfetched(monkeypatch):
    """Unfetched is not the same as empty: a 429 mistaken for 'no history' would
    shrink the ranking population and inflate every surviving rating."""
    def boom(symbols, start, cfg, budget=None, adjustment="split", client=None):
        raise rs_fetch.FetchFailed("429 too many requests")

    monkeypatch.setattr(rs_fetch, "fetch_chunk", boom)
    bars, unfetched = rs_fetch.fetch_daily_bars(["AAA", "BBB"], datetime(2025, 1, 1), {})
    assert bars == {}
    assert unfetched == {"AAA", "BBB"}


def test_one_failed_chunk_does_not_sink_the_others(monkeypatch):
    calls = {"n": 0}

    def flaky(symbols, start, cfg, budget=None, adjustment="split", client=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rs_fetch.FetchFailed("boom")
        return {s: ramp(1.0, 2.0, n=3) for s in symbols}

    monkeypatch.setattr(rs_fetch, "fetch_chunk", flaky)
    bars, unfetched = rs_fetch.fetch_daily_bars(["A", "B"], datetime(2025, 1, 1), {}, chunk=1)
    assert set(bars) == {"B"}
    assert unfetched == {"A"}


# ── Settled calendar ──────────────────────────────────────────────────────────

def test_a_partial_session_is_not_used_as_p0_before_the_settle_time():
    """Ranking half a day's move against 252 completed sessions is a different
    statistic."""
    spy = ramp(400.0, 500.0, n=10)
    today = spy.index[-1].tz_convert(ET).date()
    midday = datetime(today.year, today.month, today.day, 11, 0, tzinfo=ET)

    settled = rs_screener.settled_calendar(spy, {}, now_et=midday)
    assert len(settled) == 9
    assert settled[-1].tz_convert(ET).date() != today


def test_after_the_settle_time_todays_session_counts():
    spy = ramp(400.0, 500.0, n=10)
    today = spy.index[-1].tz_convert(ET).date()
    evening = datetime(today.year, today.month, today.day, 18, 30, tzinfo=ET)
    assert len(rs_screener.settled_calendar(spy, {}, now_et=evening)) == 10


def test_the_in_progress_session_is_kept_out_of_the_cache_entirely():
    """Not just out of the calendar — out of the STORE. A cached partial close
    moves during the session, so the next refresh sees it change by more than the
    split tolerance and repairs a symbol that never had a corporate action."""
    midday = datetime(2026, 7, 27, 11, 0, tzinfo=ET)
    assert rs_screener.last_storable_session({}, midday) == "2026-07-26"

    evening = datetime(2026, 7, 27, 18, 30, tzinfo=ET)
    assert rs_screener.last_storable_session({}, evening) == "2026-07-27"

    assert rs_screener.last_storable_session({"rs_use_partial_session": True}, midday) is None


def test_a_second_run_the_same_morning_does_not_repair_anything(monkeypatch, tmp_path):
    """The regression this guards: run, wait, run again while the tape is live.
    The only thing that changed is today's partial bar, so nothing should be
    treated as restated history."""
    data = universe_bars()
    live = datetime(2026, 7, 27, 11, 0, tzinfo=ET)

    def fetch_with_todays_partial(symbols, start, cfg, budget=None, adjustment="split",
                                  chunk=100, client=None, on_progress=None):
        out = {}
        for s in symbols:
            if s not in data:
                continue
            frame = data[s].copy()
            # Append an in-progress bar for 2026-07-27 whose close drifts.
            partial = frame.iloc[[-1]].copy()
            partial.index = pd.DatetimeIndex([pd.Timestamp("2026-07-27 13:00", tz="UTC")])
            partial["close"] = float(partial["close"].iloc[0]) * fetch_with_todays_partial.drift
            out[s] = pd.concat([frame, partial])
        return out, set()

    fetch_with_todays_partial.drift = 1.00
    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", fetch_with_todays_partial)
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")

    cfg = cfg_for(tmp_path)
    rs_screener.run_screen(cfg, write=False, now_et=live)

    fetch_with_todays_partial.drift = 1.08          # the tape moved 8% intraday
    with rs_cache.BarCache(cfg["rs_cache_path"]) as cache:
        before = cache.stats()["repairs"]
    rs_screener.run_screen(cfg, write=False, now_et=live)
    with rs_cache.BarCache(cfg["rs_cache_path"]) as cache:
        after = cache.stats()["repairs"]
        assert cache.stats()["last_session"] == "2026-07-24"

    assert after == before == 0, "an intraday move was mistaken for a split"


def test_the_partial_session_override_keeps_it():
    spy = ramp(400.0, 500.0, n=10)
    today = spy.index[-1].tz_convert(ET).date()
    midday = datetime(today.year, today.month, today.day, 11, 0, tzinfo=ET)
    settled = rs_screener.settled_calendar(spy, {"rs_use_partial_session": True},
                                           now_et=midday)
    assert len(settled) == 10


# ── run_screen ────────────────────────────────────────────────────────────────

def test_run_screen_ranks_writes_and_stamps_its_provenance(wired, tmp_path):
    payload = rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)

    assert payload["as_of"] == LAST_SESSION
    assert payload["adjustment"] == "split"
    assert payload["feed"] == "iex"
    assert payload["rs_form"] == "trailing"
    assert payload["population"] == 61                 # 60 names + SPY
    assert payload["rows"], "expected a ranked list"
    assert (tmp_path / "rs_ratings.json").exists()

    ratings = [r["rs_rating"] for r in payload["rows"]]
    assert ratings == sorted(ratings, reverse=True)
    assert all(r["population"] == 61 for r in payload["rows"])
    assert all(r["as_of"] == LAST_SESSION for r in payload["rows"])


def test_the_benchmark_rates_a_ratio_of_one_against_itself(wired, tmp_path):
    """The single best end-to-end check on the alignment: SPY compared with SPY
    must be exactly 1.0 in every window. If it is not, no other number holds."""
    payload = rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)
    spy = next(r for r in payload["rows"] if r["ticker"] == "SPY")
    for label in rs_core.RETURN_WINDOWS:
        assert spy[f"rs_vs_spy_{label}"] == pytest.approx(1.0)


def test_the_percentile_is_computed_before_the_day_trading_filters(wired, tmp_path):
    """Only the strongest names clear a high price floor; their ratings must
    still be the ones they earned against the whole population."""
    loose = rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    by_symbol = {r["ticker"]: r["rs_rating"] for r in loose["rows"]}

    strict = rs_screener.run_screen(
        cfg_for(tmp_path, rs_min_rs_rating=90), write=False, now_et=NOW_ET)

    assert 0 < len(strict["rows"]) < len(loose["rows"])
    assert strict["population"] == loose["population"]
    for row in strict["rows"]:
        assert row["rs_rating"] == by_symbol[row["ticker"]]
        assert row["population"] == loose["population"]


def test_a_missing_benchmark_aborts_the_run(monkeypatch, tmp_path):
    """Every anchor and every ratio is measured against it."""
    monkeypatch.setattr(rs_fetch, "fetch_daily_bars",
                        lambda *a, **k: ({}, set()))
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: ["AAA", "SPY"])
    with pytest.raises(rs_screener.RunRefused, match="SPY"):
        rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)


def test_a_population_below_the_floor_refuses_to_publish(wired, tmp_path):
    with pytest.raises(rs_screener.RunRefused, match="population"):
        rs_screener.run_screen(cfg_for(tmp_path, rs_min_population=5000), now_et=NOW_ET)


def test_too_many_stale_symbols_refuses_to_publish_and_keeps_the_previous_file(
        monkeypatch, tmp_path):
    """Overwriting a sound percentile with one computed over a truncated
    population is worse than serving yesterday's: it looks identical."""
    data = universe_bars()
    out_file = tmp_path / "rs_ratings.json"
    monkeypatch.setattr(rs_screener, "RS_FILE", out_file)
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))

    def good(symbols, start, cfg, budget=None, adjustment="split",
             chunk=100, client=None, on_progress=None):
        return {s: data[s].copy() for s in symbols if s in data}, set()

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", good)
    rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)
    original = out_file.read_bytes()

    # Now half the universe becomes unreachable.
    def flaky(symbols, start, cfg, budget=None, adjustment="split",
              chunk=100, client=None, on_progress=None):
        keep = [s for s in symbols if s == "SPY" or int(s[1:]) % 2 == 0]
        lost = {s for s in symbols if s not in keep}
        return {s: data[s].copy() for s in keep if s in data}, lost

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", flaky)
    with pytest.raises(rs_screener.RunRefused, match="stale|unreachable"):
        rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)

    assert out_file.read_bytes() == original, "the previous good file was clobbered"


def test_a_thin_name_is_excluded_from_the_population_rather_than_ranked(
        monkeypatch, tmp_path):
    data = universe_bars()
    data["THIN"] = ramp(10.0, 20.0, n=40)       # 40 of 253 sessions

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars",
                        lambda symbols, *a, **k: ({s: data[s].copy() for s in symbols
                                                   if s in data}, set()))
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")

    payload = rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    assert payload["thin_excluded"] >= 1
    assert "THIN" not in {r["ticker"] for r in payload["rows"]}


def test_a_rejected_candidate_names_the_filter_it_failed(wired, tmp_path):
    payload = rs_screener.run_screen(
        cfg_for(tmp_path, rs_min_price=1e9), write=False, now_et=NOW_ET)
    assert payload["rows"] == []


def test_the_run_records_how_many_names_it_could_not_reach(monkeypatch, tmp_path):
    data = universe_bars()
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")

    def one_lost(symbols, start, cfg, budget=None, adjustment="split",
                 chunk=100, client=None, on_progress=None):
        lost = {s for s in symbols if s == "S01"}
        return ({s: data[s].copy() for s in symbols if s in data and s not in lost}, lost)

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", one_lost)
    payload = rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    assert payload["stale_excluded"] >= 1
    assert any("unreachable" in d for d in payload["degraded"])


# ── Output file ───────────────────────────────────────────────────────────────

def test_the_output_is_json_serialisable_with_no_nans(wired, tmp_path):
    """json.dump writes bare NaN, which is not JSON and throws in JSON.parse."""
    rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)

    def _boom(value):
        raise AssertionError(f"non-JSON constant reached the file: {value}")

    text = (tmp_path / "rs_ratings.json").read_text(encoding="utf-8")
    payload = json.loads(text, parse_constant=_boom)
    assert payload["rows"]


def test_the_file_names_the_population_the_as_of_date_and_the_adjustment(wired, tmp_path):
    """A percentile whose population is not stated is not interpretable, and a
    price series whose adjustment is not stated cannot be compared to anything."""
    rs_screener.run_screen(cfg_for(tmp_path), now_et=NOW_ET)
    payload = json.loads((tmp_path / "rs_ratings.json").read_text(encoding="utf-8"))
    for key in ("population", "population_label", "as_of", "adjustment", "feed",
                "rs_form", "benchmark", "returns_are"):
        assert payload.get(key) not in (None, ""), key


def test_ratings_are_persisted_for_names_the_filters_dropped(wired, tmp_path):
    """So /api/rs/check can answer for a symbol that never reached the list."""
    cfg = cfg_for(tmp_path, rs_min_rs_rating=95)
    payload = rs_screener.run_screen(cfg, write=False, now_et=NOW_ET)
    served = {r["ticker"] for r in payload["rows"]}

    with rs_cache.BarCache(cfg["rs_cache_path"]) as cache:
        history = cache.rating_history("S00")
    assert "S00" not in served
    assert history and history[0]["population"] == payload["population"]


# ── Cache reuse ───────────────────────────────────────────────────────────────

def test_a_symbol_with_no_bars_is_not_re_backfilled_on_the_next_run(monkeypatch, tmp_path):
    """Otherwise ~1% of the universe re-requests a full year every single run
    and never receives anything."""
    data = universe_bars()
    asked: list[set[str]] = []

    def fetch(symbols, start, cfg, budget=None, adjustment="split",
              chunk=100, client=None, on_progress=None):
        asked.append(set(symbols))
        return {s: data[s].copy() for s in symbols if s in data}, set()

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", fetch)
    monkeypatch.setattr(rs_screener, "screen_universe",
                        lambda cfg: sorted(data) + ["NOBARS"])
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")

    rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    assert any("NOBARS" in batch for batch in asked), "first run should try it"

    asked.clear()
    rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    assert not any("NOBARS" in batch for batch in asked), "second run should not"


def test_a_second_run_reuses_the_cache_instead_of_backfilling(monkeypatch, tmp_path):
    """This is the difference between a viable daily job and a ten-minute one."""
    data = universe_bars()
    windows: list[int] = []

    def recording_fetch(symbols, start, cfg, budget=None, adjustment="split",
                        chunk=100, client=None, on_progress=None):
        windows.append((datetime.now(tz=start.tzinfo) - start).days)
        return {s: data[s].copy() for s in symbols if s in data}, set()

    monkeypatch.setattr(rs_fetch, "fetch_daily_bars", recording_fetch)
    monkeypatch.setattr(rs_screener, "screen_universe", lambda cfg: sorted(data))
    monkeypatch.setattr(rs_screener, "RS_FILE", tmp_path / "rs_ratings.json")

    rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)
    first_run = list(windows)
    windows.clear()
    rs_screener.run_screen(cfg_for(tmp_path), write=False, now_et=NOW_ET)

    assert max(first_run) >= 400, "the first run should backfill the full window"
    assert max(windows) < max(first_run), "the second run should ask for far less"


# ── Scheduler ─────────────────────────────────────────────────────────────────

def test_seconds_until_next_run_picks_the_next_slot_today():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=ET)
    seconds = rs_screener._seconds_until_next_run({"rs_run_times": ["18:30"]}, now)
    assert seconds == pytest.approx(6.5 * 3600)


def test_seconds_until_next_run_rolls_to_tomorrow_after_the_last_slot():
    now = datetime(2026, 7, 27, 20, 0, tzinfo=ET)
    seconds = rs_screener._seconds_until_next_run({"rs_run_times": ["18:30"]}, now)
    assert seconds == pytest.approx(22.5 * 3600)


def test_the_default_schedule_is_once_a_day():
    """RS is a 12-month statistic — recomputing it midday is churn, not news."""
    from config import DEFAULT_CONFIG
    assert len(DEFAULT_CONFIG["rs_run_times"]) == 1


def test_the_market_being_open_is_detected_so_a_backfill_can_be_deferred():
    assert rs_screener._market_is_open(datetime(2026, 7, 27, 10, 0, tzinfo=ET)) is True
    assert rs_screener._market_is_open(datetime(2026, 7, 27, 21, 0, tzinfo=ET)) is False
    assert rs_screener._market_is_open(datetime(2026, 7, 25, 10, 0, tzinfo=ET)) is False


# ── Config contract ───────────────────────────────────────────────────────────

def test_every_rs_default_is_exposed_to_the_config_api():
    from config import DEFAULT_CONFIG, SAFE_CONFIG_KEYS
    rs_keys = {k for k in DEFAULT_CONFIG if k.startswith("rs_")}
    assert rs_keys <= set(SAFE_CONFIG_KEYS)


def test_the_dead_finviz_keys_are_gone():
    """They were read by nothing, were absent from DEFAULT_CONFIG and
    SAFE_CONFIG_KEYS, and save_config re-persisted them on every write."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    saved = json.loads((root / "config" / "bot_config.json").read_text(encoding="utf-8"))
    assert not [k for k in saved if k.startswith("finviz_")]


def test_raw_bars_are_not_a_reachable_configuration():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["rs_bar_adjustment"] == "split"
