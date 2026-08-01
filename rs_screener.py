#!/usr/bin/env python3
"""
rs_screener.py — IBD-style relative-strength screener.

A third kind of name for the desk. The momentum table and the Stocktwits panel
both answer "what is hot right now"; this answers "what has actually been
beating the market for months", which is the question you want answered before
deciding which hot name deserves the capital.

Data source (no paid third party):
  • Alpaca — SPLIT-ADJUSTED daily bars for the universe and for SPY, cached in
    SQLite (rs_cache.py) so a daily run refetches one session, not a year.

Funnel:
  1. Universe = every tradable US equity Alpaca knows (~13,290), or a Finviz
     screen, or an explicit rs_universe.json override.
  2. SPY first — it defines the session calendar every other symbol is aligned
     to. No SPY, no run.
  3. Refresh the cache in two buckets: unknown symbols get the full backfill
     window, known ones get last_session minus a few sessions of overlap. That
     overlap is the split detector (see rs_cache.py).
  4. Rank the WHOLE bar-covered universe into a 1-99 percentile.
  5. THEN apply the day-trading filters, cap, and write rs_ratings.json.

  Step 4 before step 5 is the point. A percentile taken over the survivors of a
  strength screen would make "RS 90" mean "top 10% of names that already look
  strong" — a different and much weaker claim than "top 10% of the market". The
  same critique is written down for medians at stocktwits_trending.py:502-505.
  Every row carries the population it was ranked against so this stays checkable.

Runs once a day (default 18:30 ET), NOT on a loop and NOT three times a session:
RS is a 12-month statistic, so recomputing it at 08:00 and again at 12:30 would
produce churn carrying no information. `as_of` is always a COMPLETED session.

Free-tier limitations, stated plainly:
  • Bars are IEX, not consolidated — avg_vol_50d is IEX volume and is a fraction
    of the tape. Calibrate rs_min_avg_vol_50d accordingly.
  • Adjustment.SPLIT excludes dividends, so these are PRICE returns. A 4%-yielder's
    total return is ~4pp above what is reported here.
  • Alpaca's asset list has no ETF flag, so the ranked population includes ETPs,
    ADRs and preferreds. See rs_exclude_etp.
  • The RS formula is an approximation. IBD's real one is proprietary.

Standalone:
    python rs_screener.py --once
    python rs_screener.py --explain NVDA AAPL SPY
    python rs_screener.py --rebuild          # drop the cache and backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import rs_cache
import rs_core
import rs_fetch
from config import load_config

ET = ZoneInfo("America/New_York")
log = logging.getLogger("rs")

RS_FILE = Path(__file__).parent / "rs_ratings.json"
UNIVERSE_FILE = Path(__file__).parent / "rs_universe.json"

POPULATION_LABEL = "bar-covered tradable US equities and ETPs, Alpaca IEX, split-adjusted"

# Published for /api/rs so a refresh button is not silent for ten minutes.
_PROGRESS: dict = {"running": False, "chunks_done": 0, "chunks_total": 0, "phase": "idle"}
_PROGRESS_LOCK = threading.Lock()


def progress() -> dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _set_progress(**kw) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)


def resolve_credentials(cfg: dict) -> dict:
    """Fill api_key/secret_key from signal_engine.env when load_config has none.

    config.load_config() looks in env vars (read at import), config/bot_config.json
    and config/secrets.json — but not in signal_engine.env, which is where a
    machine set up from signal_engine.env.example actually keeps its Alpaca keys.
    Without this the screener dies on "You must supply a method of authentication"
    on exactly the checkouts that are otherwise fully configured.

    Reuses trade_bridge.config._parse_env_file rather than hand-rolling another
    KEY=VALUE reader: that parser is already copy-pasted into eight modules, and
    a ninth would be one more place for the quoting rules to drift.
    """
    if cfg.get("api_key") and cfg.get("secret_key"):
        return cfg
    try:
        from trade_bridge.config import ENGINE_ENV_FILE, _parse_env_file
        env = _parse_env_file(ENGINE_ENV_FILE)
    except Exception as exc:                               # noqa: BLE001
        log.debug("[rs] signal_engine.env unreadable: %s", exc)
        return cfg
    cfg = dict(cfg)
    cfg["api_key"] = cfg.get("api_key") or env.get("ALPACA_API_KEY", "")
    cfg["secret_key"] = cfg.get("secret_key") or env.get("ALPACA_SECRET_KEY", "")
    return cfg


class RunRefused(RuntimeError):
    """The run produced a result we are not willing to publish.

    Overwriting a sound percentile with one computed over a truncated population
    is worse than serving yesterday's: the new file looks identical and means
    something different.
    """


# ── Universe ──────────────────────────────────────────────────────────────────

def screen_universe(cfg: dict) -> list[str]:
    """Symbols to rank, in priority order: explicit override, then the
    configured source, then the Alpaca tradable list as the fallback."""
    if UNIVERSE_FILE.exists():
        try:
            symbols = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            if isinstance(symbols, list) and symbols:
                log.info("[rs] using rs_universe.json override (%d symbols)", len(symbols))
                return [str(s).upper() for s in symbols]
        except Exception as exc:                           # noqa: BLE001
            log.warning("[rs] bad rs_universe.json (%s) — ignoring", exc)

    source = str(cfg.get("rs_universe_source", "alpaca")).lower()
    if source == "finviz":
        try:
            import finviz_universe
            symbols = finviz_universe.fetch_universe(cfg)
        except Exception as exc:                           # noqa: BLE001
            log.warning("[rs] finviz universe failed (%s)", exc)
            symbols = []
        if symbols:
            return symbols
        # A broken scrape must not end the run — fall through to Alpaca.
        log.warning("[rs] finviz returned nothing — falling back to the Alpaca universe")

    return rs_fetch.tradable_universe(cfg)


# ── Session calendar ──────────────────────────────────────────────────────────

def settled_calendar(spy: pd.DataFrame, cfg: dict, now_et: datetime | None = None
                     ) -> pd.DatetimeIndex:
    """SPY's session index, with today dropped until the tape has settled.

    Alpaca will happily serve a partial bar for a session in progress. Ranking a
    half-day's move against 252 completed ones is not the same statistic, so
    unless rs_use_partial_session is on, today's bar is excluded until
    rs_settle_after (18:00 ET by default — well past the close and late prints).
    """
    if spy is None or spy.empty:
        return pd.DatetimeIndex([])
    calendar = pd.DatetimeIndex(spy.index).sort_values()
    if cfg.get("rs_use_partial_session", False):
        return calendar

    now_et = now_et or datetime.now(ET)
    settle = str(cfg.get("rs_settle_after", "18:00"))
    try:
        hour, minute = (int(part) for part in settle.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 18, 0

    if now_et.hour * 60 + now_et.minute >= hour * 60 + minute:
        return calendar

    today = now_et.date()
    keep = [ts for ts in calendar if ts.tz_convert(ET).date() != today]
    return pd.DatetimeIndex(keep)


def last_storable_session(cfg: dict, now_et: datetime | None = None) -> str | None:
    """Newest ET session the cache may hold, or None for no limit.

    Everything in this design is measured on COMPLETED sessions, and the cache
    must match. Alpaca serves a partial bar for a session in progress, so
    caching it means the next refresh compares one partial close against a later
    partial close of the same day, exceeds the split tolerance, and repairs a
    symbol that never split. Observed live: 78 spurious repairs across a
    1,500-name universe from two runs twenty minutes apart.
    """
    if cfg.get("rs_use_partial_session", False):
        return None
    now_et = now_et or datetime.now(ET)
    settle = str(cfg.get("rs_settle_after", "18:00"))
    try:
        hour, minute = (int(part) for part in settle.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 18, 0
    if now_et.hour * 60 + now_et.minute >= hour * 60 + minute:
        return now_et.date().isoformat()
    return (now_et.date() - timedelta(days=1)).isoformat()


def _lookback_start_session(calendar: pd.DatetimeIndex, sessions: int) -> str | None:
    """The ISO session `sessions` back, used to bound cache reads and the trim."""
    if len(calendar) == 0:
        return None
    idx = max(0, len(calendar) - 1 - int(sessions))
    return calendar[idx].tz_convert(ET).date().isoformat()


# ── Cache refresh ─────────────────────────────────────────────────────────────

def refresh_cache(cache: rs_cache.BarCache, symbols: list[str], cfg: dict,
                  budget: rs_fetch.RateBudget,
                  now_et: datetime | None = None) -> tuple[set[str], int]:
    """Bring every symbol up to date. Returns (unfetched, repairs).

    Two buckets, and the split must be explicit: without it you either backfill
    the whole universe every run or never pick up a new listing.
    """
    adjustment = str(cfg.get("rs_bar_adjustment", "split"))
    chunk = int(cfg.get("rs_chunk_size", rs_fetch.DEFAULT_CHUNK))
    overlap = int(cfg.get("rs_overlap_sessions", 5))
    tolerance = float(cfg.get("rs_split_tolerance", rs_cache.DEFAULT_SPLIT_TOLERANCE))
    backfill_days = int(cfg.get("rs_backfill_calendar_days", 400))
    # The in-progress session never enters the cache — see last_storable_session.
    max_session = last_storable_session(cfg, now_et)

    known = cache.known_symbols()
    last_by_symbol = cache.last_sessions()
    # Symbols the vendor had nothing for earlier today are not re-asked. About 1%
    # of the tradable list (ADRs, thin foreign listings) has no IEX history at
    # all, and without this they look like new listings forever.
    skip = cache.recently_empty()
    backfill = [s for s in symbols
                if (s not in known or not last_by_symbol.get(s)) and s not in skip]
    incremental = [s for s in symbols if last_by_symbol.get(s)]

    unfetched: set[str] = set()
    repairs = 0

    # -- incremental: fetch the overlap too, because the overlap IS the detector
    if incremental:
        oldest = min(last_by_symbol[s] for s in incremental)
        start = datetime.fromisoformat(oldest) - timedelta(days=overlap * 2 + 5)
        _set_progress(phase="refresh")
        # The daily path fetches only a handful of sessions per symbol, so the
        # 10,000-bar page cap is nowhere near binding and a much wider batch
        # still costs one page. This is the one place chunk size genuinely cuts
        # the request count — on a backfill it does not, because the cap counts
        # bars rather than symbols.
        fetched, failed = rs_fetch.fetch_daily_bars(
            incremental, start, cfg, budget, adjustment,
            chunk=rs_fetch.chunk_size_for(
                overlap * 3, int(cfg.get("rs_incremental_chunk_size", 300))),
            on_progress=lambda i, n: _set_progress(chunks_done=i, chunks_total=n))
        unfetched |= failed

        needs_refull: list[str] = []
        for symbol, frame in fetched.items():
            new_closes = {rs_cache._session_key(ts): float(v)
                          for ts, v in frame["close"].items()}
            # Compare settled bars only, for the same reason we store only
            # settled bars: a partial close moves during the session.
            if max_session:
                new_closes = {k: v for k, v in new_closes.items() if k <= max_session}
            old_closes = cache.closes(symbol, since=min(new_closes) if new_closes else None)
            if old_closes and rs_cache.needs_repair(old_closes, new_closes, tolerance):
                ratio = rs_cache.split_ratio(old_closes, new_closes)
                log.info("[rs] %s: history restated (x%.4g) — refulling", symbol, ratio or 0.0)
                cache.purge(symbol)
                cache.record_repair(symbol, ratio)
                needs_refull.append(symbol)
            else:
                cache.upsert(symbol, frame, max_session)
        repairs = len(needs_refull)
        backfill.extend(needs_refull)

    # -- backfill: new listings, repaired symbols, and the very first run
    if backfill:
        _set_progress(phase="backfill")
        start = rs_fetch.lookback_start(backfill_days)
        fetched, failed = rs_fetch.fetch_daily_bars(
            backfill, start, cfg, budget, adjustment,
            chunk=rs_fetch.chunk_size_for(backfill_days, chunk),
            on_progress=lambda i, n: _set_progress(chunks_done=i, chunks_total=n))
        unfetched |= failed
        for symbol, frame in fetched.items():
            cache.upsert(symbol, frame, max_session)
        # Note the ones the vendor simply has no history for, so tomorrow's run
        # does not spend the same requests learning the same nothing.
        for symbol in backfill:
            if symbol not in fetched and symbol not in failed:
                cache.mark_empty(symbol)

    return unfetched, repairs


# ── ETP strip ─────────────────────────────────────────────────────────────────

def strip_etps(rows: list[dict], cfg: dict, degraded: list[str]) -> tuple[list[dict], int]:
    """Drop ETFs/ETPs from the SERVED list — never from the ranked population.

    Alpaca's asset list cannot tell a stock from a leveraged ETP, and in a strong
    tape the top of an unfiltered RS list fills with the TQQQ/SOXL family. IBD
    ranks common stock only.

    The test is the one swing_screener.score_candidate:481 already uses: ask
    Finnhub for a profile, and a name it answers about but has no industry for
    is an ETF or uncovered. Conservative by construction — a name is only
    dropped when Finnhub AFFIRMATIVELY answered. If the endpoint is gated or
    the key is missing, nothing is stripped and the run says so, because
    guessing from a ticker's name is how you drop a real stock.
    """
    if not cfg.get("rs_exclude_etp", True) or not rows:
        return rows, 0
    if not cfg.get("finnhub_key"):
        degraded.append("etp-filter: no finnhub_key — ETPs not stripped")
        return rows, 0

    try:
        from swing_screener import _Degraded, _finnhub_get
    except Exception as exc:                               # noqa: BLE001
        degraded.append(f"etp-filter unavailable: {exc}")
        return rows, 0

    kept: list[dict] = []
    dropped = 0
    gated = 0
    for row in rows:
        symbol = row.get("ticker")
        try:
            profile = _finnhub_get("stock/profile2", cfg, symbol=symbol) or {}
        except _Degraded:
            gated += 1
            kept.append(row)                    # cannot tell → keep it
            continue
        except Exception:                                  # noqa: BLE001
            kept.append(row)
            continue
        if isinstance(profile, dict) and profile and not profile.get("finnhubIndustry"):
            dropped += 1
            continue
        kept.append(row)
    if gated:
        degraded.append(f"etp-filter: {gated} lookups gated by Finnhub")
    return kept, dropped


# ── The screen ────────────────────────────────────────────────────────────────

def run_screen(cfg: dict, write: bool = True, now_et: datetime | None = None) -> dict:
    """Universe → cache → rank the whole population → filter → rs_ratings.json.

    Returns the full payload (header plus rows). Raises RunRefused when the
    result is not fit to publish.
    """
    started = time.time()
    _set_progress(running=True, phase="universe", chunks_done=0, chunks_total=0)
    degraded: list[str] = []
    try:
        cfg = resolve_credentials(cfg)
        benchmark = str(cfg.get("rs_benchmark", "SPY")).upper()
        form = str(cfg.get("rs_form", "trailing"))
        lookback = int(cfg.get("rs_lookback_sessions", 252))
        budget = rs_fetch.RateBudget(int(cfg.get("rs_max_req_per_min",
                                                 rs_fetch.DEFAULT_MAX_PER_MIN)))

        symbols = [s for s in screen_universe(cfg) if s and "/" not in s]
        if benchmark not in symbols:
            symbols.append(benchmark)
        log.info("[rs] universe = %d symbols", len(symbols))

        cache = rs_cache.BarCache(
            _cache_path(cfg),
            adjustment=str(cfg.get("rs_bar_adjustment", "split")),
            feed="iex")
        try:
            # -- the benchmark defines the calendar; without it nothing aligns
            _set_progress(phase="benchmark")
            bench_unfetched, _ = refresh_cache(cache, [benchmark], cfg, budget, now_et)
            spy = cache.get(benchmark)
            if benchmark in bench_unfetched or spy.empty:
                raise RunRefused(f"no {benchmark} history — every anchor and every "
                                 f"ratio is measured against it")

            calendar = settled_calendar(spy, cfg, now_et)
            if len(calendar) < lookback + 1:
                raise RunRefused(f"{benchmark} has {len(calendar)} settled sessions, "
                                 f"need {lookback + 1}")
            calendar = calendar[-(lookback + 1):]
            as_of = calendar[-1].tz_convert(ET).date().isoformat()
            log.info("[rs] as_of=%s  sessions=%d", as_of, len(calendar))

            # -- refresh everything else
            unfetched, repairs = refresh_cache(cache, symbols, cfg, budget, now_et)
            if unfetched:
                degraded.append(f"{len(unfetched)} symbols unreachable this run")
            if repairs:
                log.info("[rs] repaired %d symbols after a restated history", repairs)

            # -- benchmark returns, computed on the same calendar as everyone else
            _set_progress(phase="rank")
            spy_aligned, _ = rs_core.align_frame_to_calendar(
                spy, calendar, int(cfg.get("rs_ffill_limit", 2)))
            bench_returns = {f"ret_{label}": rs_core.period_return(spy_aligned["close"], n)
                             for label, n in rs_core.RETURN_WINDOWS.items()}

            # -- build every row, over the whole population
            since = _lookback_start_session(calendar, lookback + 10)
            bars_by_symbol = cache.get_many(symbols, since=since)
            rows, stale, thin = _build_rows(bars_by_symbol, symbols, unfetched, calendar,
                                            bench_returns, cfg, form, as_of)

            population = len(rows)
            min_population = int(cfg.get("rs_min_population", 500))
            if population < min_population:
                raise RunRefused(f"only {population} symbols survived to the ranking "
                                 f"(floor {min_population}) — a percentile over this "
                                 f"population would not mean what it says")

            considered = population + len(stale)
            stale_frac = len(stale) / considered if considered else 0.0
            max_stale = float(cfg.get("rs_max_stale_frac", 0.10))
            if stale_frac > max_stale:
                raise RunRefused(f"{stale_frac:.1%} of the universe was stale or "
                                 f"unreachable (limit {max_stale:.0%}) — refusing to "
                                 f"overwrite a good file with a truncated ranking")

            # -- rank the population BEFORE any day-trading filter
            raw = {r["ticker"]: r["rs_raw"] for r in rows if r["rs_raw"] is not None}
            ratings = rs_core.percentile_ratings(raw)
            percentiles = rs_core.rank_percentiles(raw)
            rs_core.stamp_ratings(rows, ratings, percentiles, population, as_of)
            cache.write_ratings(as_of, rows, form, population)

            # -- now filter
            _set_progress(phase="filter")
            kept: list[dict] = []
            for row in rows:
                ok, rejects = rs_core.passes_filters(row, cfg)
                row["rejects"] = rejects
                if ok:
                    kept.append(row)
            ranked = rs_core.rank_and_cap(kept, int(cfg.get("rs_limit", 100)))
            ranked, excluded_etp = strip_etps(ranked, cfg, degraded)

            payload = {
                "updated": time.time(),
                "as_of": as_of,
                "benchmark": benchmark,
                "benchmark_sessions": len(calendar),
                "windows": dict(rs_core.RETURN_WINDOWS),
                "rs_anchors": sorted(rs_core.RS_WEIGHTS),
                "rs_weights": {str(k): v for k, v in rs_core.RS_WEIGHTS.items()},
                "rs_form": form,
                "population": population,
                "population_label": POPULATION_LABEL,
                "population_excluded_etp": excluded_etp,
                "rated": len(raw),
                "stale_excluded": len(stale),
                "thin_excluded": len(thin),
                "adjustment": str(cfg.get("rs_bar_adjustment", "split")),
                "feed": "iex",
                "returns_are": "price only — Adjustment.SPLIT excludes dividends",
                "degraded": degraded,
                "elapsed_sec": round(time.time() - started, 1),
                "pages_used": budget.pages,
                "spy": bench_returns,
                "rows": ranked,
            }

            if write:
                _trim(cache, calendar, lookback)
                _write_rs(payload)
                log.info("[rs] wrote %d of %d ranked in %.1fs (%d pages)",
                         len(ranked), population, payload["elapsed_sec"], budget.pages)
            return payload
        finally:
            cache.close()
    finally:
        _set_progress(running=False, phase="idle")


def _build_rows(bars_by_symbol: dict, symbols: list[str], unfetched: set[str],
                calendar: pd.DatetimeIndex, bench_returns: dict, cfg: dict,
                form: str, as_of: str) -> tuple[list[dict], list[str], list[str]]:
    """(rows, stale, thin) — one row per symbol fit to be ranked.

    Three ways out of the population, all of them counted rather than silent:
      • unfetched — the batch errored, so we do not know anything about it;
      • stale — its last real bar is older than as_of, so its returns would end
        on a different session than everyone else's;
      • thin — too few real bars against the benchmark calendar to align safely.
    """
    ffill = int(cfg.get("rs_ffill_limit", 2))
    min_coverage = float(cfg.get("rs_min_coverage", 0.80))
    max_staleness = int(cfg.get("rs_max_p0_staleness_sessions", 1))
    session_of = {ts: i for i, ts in enumerate(calendar)}
    last_index = len(calendar) - 1

    rows: list[dict] = []
    stale: list[str] = []
    thin: list[str] = []

    for symbol in symbols:
        if symbol in unfetched:
            stale.append(symbol)
            continue
        frame = bars_by_symbol.get(symbol)
        if frame is None or frame.empty:
            thin.append(symbol)
            continue
        try:
            aligned, coverage = rs_core.align_frame_to_calendar(frame, calendar, ffill)
            if coverage < min_coverage:
                thin.append(symbol)
                continue

            real = aligned.index[aligned["real_bar"].astype(bool)]
            if len(real) == 0:
                thin.append(symbol)
                continue
            if last_index - session_of.get(real[-1], -1) > max_staleness:
                stale.append(symbol)
                continue

            rows.append(rs_core.build_row(symbol, aligned, coverage, bench_returns, form))
        except Exception as exc:                           # noqa: BLE001
            log.warning("[rs] %s skipped: %s", symbol, exc)
            thin.append(symbol)
    return rows, stale, thin


def _trim(cache: rs_cache.BarCache, calendar: pd.DatetimeIndex, lookback: int) -> None:
    keep_from = _lookback_start_session(calendar, lookback + 30)
    if keep_from:
        removed = cache.trim(keep_from)
        if removed:
            log.info("[rs] trimmed %d bars older than %s", removed, keep_from)


def _cache_path(cfg: dict) -> Path:
    path = Path(str(cfg.get("rs_cache_path", "rs_cache.sqlite")))
    return path if path.is_absolute() else Path(__file__).parent / path


def _write_rs(payload: dict) -> None:
    """Atomic write, mirroring swing_screener._write_swing:672.

    rs_core.jsonable() runs first: json.dump emits bare NaN, which is not JSON
    and throws in the browser's JSON.parse.
    """
    RS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=RS_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rs_core.jsonable(payload), handle, indent=2, allow_nan=False)
        except Exception:
            os.close(fd)
            raise
        Path(tmp_path).replace(RS_FILE)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _parse_run_times(cfg: dict) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for entry in cfg.get("rs_run_times", ["18:30"]):
        try:
            hour, minute = str(entry).split(":")
            out.append((int(hour), int(minute)))
        except (ValueError, AttributeError):
            log.warning("[rs] bad run time %r — ignored", entry)
    return sorted(out) or [(18, 30)]


def _seconds_until_next_run(cfg: dict, now: datetime | None = None) -> float:
    now = now or datetime.now(ET)
    todays = [now.replace(hour=h, minute=m, second=0, microsecond=0)
              for h, m in _parse_run_times(cfg)]
    upcoming = [t for t in todays if t > now]
    nxt = upcoming[0] if upcoming else todays[0] + timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


def _market_is_open(now_et: datetime | None = None) -> bool:
    """A backfill is ~400 requests on a key the live engine shares. Not during RTH."""
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 4 * 60 <= minutes < 20 * 60      # 04:00-20:00 ET, extended hours included


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_explain(payload: dict, wanted: list[str], cfg: dict) -> None:
    by_symbol = {r["ticker"]: r for r in payload["rows"]}
    print(f"\nas_of {payload['as_of']}  ·  population {payload['population']:,}  "
          f"·  form {payload['rs_form']}  ·  {payload['adjustment']}/{payload['feed']}")
    print(f"{payload['population_label']}\n")
    for symbol in wanted:
        row = by_symbol.get(symbol.upper())
        if row is None:
            print(f"{symbol.upper():6s}  not in the served list "
                  f"(filtered out, or not in the universe)")
            continue
        print(f"{row['ticker']:6s}  RS {row['rs_rating'] or '—'}  "
              f"raw {row['rs_raw']:.4f}  pct {row['rs_percentile']}")
        for label in rs_core.RETURN_WINDOWS:
            ret, ratio = row.get(f"ret_{label}"), row.get(f"rs_vs_spy_{label}")
            print(f"        {label:>4s}  ret {_pct(ret):>9s}   vs SPY {_x(ratio):>8s}")
        print(f"        price {row['price']}  sma50 {row['sma50']}  sma200 {row['sma200']}")
        print(f"        avg_vol_50d {row['avg_vol_50d']}  rvol {row['rvol']}  "
              f"adr {row['adr_pct']}")
        print(f"        coverage {row['coverage']}  sessions {row['sessions_available']}  "
              f"p0 {row['p0_date']}")
        if row.get("insufficient"):
            print(f"        insufficient: {', '.join(row['insufficient'])}")
        if row.get("rejects"):
            print(f"        rejects: {'; '.join(row['rejects'])}")
        print()


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def _x(value) -> str:
    return "—" if value is None else f"{value:.3f}x"


def main() -> None:
    parser = argparse.ArgumentParser(description="IBD-style relative-strength screener")
    parser.add_argument("--once", action="store_true", help="run one screen and exit")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete the bar cache and backfill from scratch")
    parser.add_argument("--explain", nargs="+", metavar="SYM",
                        help="run a screen and print every field for these symbols")
    parser.add_argument("--force", action="store_true",
                        help="run even while the market is open")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config()

    if args.rebuild:
        path = _cache_path(cfg)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()
                log.info("[rs] removed %s", candidate.name)

    one_shot = args.once or args.rebuild or args.explain
    if one_shot:
        if _market_is_open() and not args.force:
            log.warning("[rs] the market is open — a backfill is ~400 requests on the "
                        "same key the engine uses. Re-run after 20:00 ET, or --force.")
            return
        payload = run_screen(cfg, write=not args.explain)
        if args.explain:
            _print_explain(payload, args.explain, cfg)
        return

    log.info("[rs] started — scheduled runs at %s ET (sleeps in between)",
             ", ".join(str(t) for t in cfg.get("rs_run_times", ["18:30"])))
    while True:
        cfg = load_config()
        sleep_s = _seconds_until_next_run(cfg)
        log.info("[rs] next run in %.1f min", sleep_s / 60.0)
        time.sleep(sleep_s)
        try:
            run_screen(load_config())
        except RunRefused as exc:
            log.warning("[rs] refused to publish: %s", exc)
        except Exception as exc:                           # noqa: BLE001
            log.warning("[rs] run failed: %s", exc)
        time.sleep(60)          # clear the trigger minute before recomputing


if __name__ == "__main__":
    main()
