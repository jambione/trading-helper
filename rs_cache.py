#!/usr/bin/env python3
"""
rs_cache.py — SQLite daily-bar store for the relative-strength screener.

The RS calculation needs 252 sessions of history for ~13k symbols. Refetching
that every run is ~3.6M bars and several minutes of wall clock; refetching one
session is ~13k bars and a few seconds. That difference is the whole reason this
module exists — it is not an optimisation, it is what makes a daily job viable.

No Alpaca, no config, no clock: this module stores bars and detects when the
vendor has restated them. rs_fetch.py does the fetching, rs_screener.py owns the
policy. Two of its functions (split_ratio, needs_repair) are pure and testable
with two plain dicts.

WHY SQLITE AND NOT PARQUET
    pyarrow is a ~50 MB dependency that is not installed, and this repo has no
    binary caches at all — everything else is JSON/JSONL. sqlite3 is stdlib, and
    the access pattern (range-scan one symbol's history, upsert one session) is
    exactly what a clustered primary key is for.

THE SPLIT PROBLEM
    Alpaca's daily bars default to Adjustment.RAW; we store Adjustment.SPLIT.
    But split adjustment is retroactive — the day a stock splits 2:1, every
    historical bar the vendor serves is halved, and a cache holding yesterday's
    numbers is now inconsistent with today's. On a 252-session lookback that
    turns a routine corporate action into a fake ±50-90% return, and the
    affected names land at the extremes of the percentile, i.e. at the top of
    the ranked output.

    So every incremental refresh deliberately re-requests the last few sessions
    it already holds. If those overlapping closes come back different, the
    vendor restated history and the symbol is purged and refetched in full.
    The overlap is not redundancy — it is the detector.

    We never apply the adjustment factor locally. Re-implementing the vendor's
    arithmetic will drift from it; purge and refetch cannot.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger("rs.cache")

ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = "1"

# A 3:2 split is +50%; a genuine late-print revision is a fraction of a cent.
# 0.5% sits between them with room to spare, and on a $0.30 stock it is still
# below one tick.
DEFAULT_SPLIT_TOLERANCE = 0.005

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT NOT NULL,
    session TEXT NOT NULL,
    close   REAL NOT NULL,
    high    REAL,
    low     REAL,
    volume  REAL,
    PRIMARY KEY (symbol, session)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS symbol_meta (
    symbol            TEXT PRIMARY KEY,
    adjustment        TEXT    NOT NULL,
    feed              TEXT    NOT NULL,
    first_session     TEXT,
    last_session      TEXT,
    refreshed_at      REAL,
    repairs           INTEGER NOT NULL DEFAULT 0,
    last_repair_ratio REAL
);

CREATE TABLE IF NOT EXISTS ratings (
    as_of      TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    rs_rating  INTEGER,
    rs_raw     REAL,
    rs_form    TEXT    NOT NULL,
    population INTEGER NOT NULL,
    PRIMARY KEY (as_of, symbol)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class AdjustmentMismatch(RuntimeError):
    """The cache holds bars adjusted differently from what this run wants.

    Raised rather than silently refetching: a 10-minute rebuild triggered by a
    config typo is worse than an error message telling you to pass --rebuild.
    """


# ── Pure helpers (no disk — the split arithmetic, testable with two dicts) ────

def split_ratio(cached: dict[str, float], fetched: dict[str, float]) -> float | None:
    """Median fetched/cached across sessions both sides hold, or None if none overlap.

    The median rather than the mean: one bad bar in the overlap should not move
    the reported signature, which is what gets logged for audit.
    """
    ratios = [fetched[s] / cached[s]
              for s in cached.keys() & fetched.keys()
              if cached.get(s) not in (None, 0) and fetched.get(s) is not None]
    return float(statistics.median(ratios)) if ratios else None


def needs_repair(cached: dict[str, float], fetched: dict[str, float],
                 tolerance: float = DEFAULT_SPLIT_TOLERANCE) -> bool:
    """True when any overlapping session's close moved by more than `tolerance`.

    Close only. Volume also scales across a split, but volume revisions are
    routine and using them as a trigger produces false repairs.
    """
    for session in cached.keys() & fetched.keys():
        old, new = cached.get(session), fetched.get(session)
        if old in (None, 0) or new is None:
            continue
        if abs(new / old - 1.0) > tolerance:
            return True
    return False


# ── Store ─────────────────────────────────────────────────────────────────────

class BarCache:
    """Daily bars keyed by (symbol, ET session date).

    `session` is stored as an ISO 'YYYY-MM-DD' string so lexical order is
    chronological, it joins straight to date.isoformat(), and it is legible in
    the sqlite3 CLI. It is the EASTERN session date, not the UTC one: Alpaca
    stamps daily bars at 04:00 or 05:00 UTC depending on daylight saving, so
    keying on the UTC date would file half the year's bars one day early.
    """

    def __init__(self, path: str | Path, adjustment: str = "split", feed: str = "iex"):
        self.path = Path(path)
        self.adjustment = str(adjustment).lower()
        self.feed = str(feed).lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- lifecycle -------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._conn
        # WAL so the dashboard can read the ratings table while a screen writes.
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA temp_store = MEMORY")
        cur.execute("PRAGMA cache_size = -64000")       # 64 MB
        cur.executescript(_SCHEMA)
        cur.commit()

        stored = self.get_meta("adjustment")
        if stored is None:
            self.set_meta("adjustment", self.adjustment)
            self.set_meta("feed", self.feed)
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif stored != self.adjustment:
            raise AdjustmentMismatch(
                f"{self.path.name} holds {stored!r}-adjusted bars but this run wants "
                f"{self.adjustment!r}. Mixing them would compare prices on two "
                f"different bases. Rebuild the cache (--rebuild) to switch."
            )

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:                                  # noqa: BLE001
            pass

    def __enter__(self) -> BarCache:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO cache_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        self._conn.commit()

    def symbol_meta(self, symbol: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM symbol_meta WHERE symbol = ?",
                                 (symbol,)).fetchone()
        return dict(row) if row else None

    def known_symbols(self) -> set[str]:
        """Symbols that already have history — the incremental bucket. Anything
        not in here needs a full backfill, including yesterday's new listings."""
        return {r["symbol"] for r in
                self._conn.execute("SELECT symbol FROM symbol_meta")}

    def last_session(self, symbol: str) -> str | None:
        meta = self.symbol_meta(symbol)
        return meta.get("last_session") if meta else None

    def last_sessions(self) -> dict[str, str]:
        """{symbol → last cached session} for every known symbol, in one query.

        Per-symbol lookups would be ~13k round trips at the top of every run.
        """
        return {r["symbol"]: r["last_session"] for r in
                self._conn.execute(
                    "SELECT symbol, last_session FROM symbol_meta WHERE last_session IS NOT NULL")}

    # -- reads -----------------------------------------------------------------

    def closes(self, symbol: str, since: str | None = None) -> dict[str, float]:
        """{session → close} — the shape the split detector compares."""
        sql = "SELECT session, close FROM bars WHERE symbol = ?"
        args: list = [symbol]
        if since:
            sql += " AND session >= ?"
            args.append(since)
        return {r["session"]: float(r["close"])
                for r in self._conn.execute(sql, args)}

    def get(self, symbol: str, since: str | None = None) -> pd.DataFrame:
        """Oldest→newest OHLCV, indexed by ET session midnight.

        The index is the point. swing_screener._clean_bars ends in
        reset_index(drop=True), which is why it cannot be reused here: without
        dates, aligning a gappy symbol to the benchmark calendar is impossible
        and every anchor silently shifts.
        """
        sql = "SELECT session, close, high, low, volume FROM bars WHERE symbol = ?"
        args: list = [symbol]
        if since:
            sql += " AND session >= ?"
            args.append(since)
        sql += " ORDER BY session"
        rows = self._conn.execute(sql, args).fetchall()
        if not rows:
            return pd.DataFrame(columns=["close", "high", "low", "volume"])
        frame = pd.DataFrame([dict(r) for r in rows])
        frame.index = _session_index(frame.pop("session"))
        return frame

    def get_many(self, symbols: list[str], since: str | None = None) -> dict[str, pd.DataFrame]:
        """{symbol → frame} in a single scan rather than one query per symbol."""
        if not symbols:
            return {}
        wanted = set(symbols)
        sql = "SELECT symbol, session, close, high, low, volume FROM bars"
        args: list = []
        if since:
            sql += " WHERE session >= ?"
            args.append(since)
        sql += " ORDER BY symbol, session"

        out: dict[str, pd.DataFrame] = {}
        buffer: list[dict] = []
        current: str | None = None

        def _flush() -> None:
            if current is not None and buffer:
                frame = pd.DataFrame(buffer)
                frame.index = _session_index(frame.pop("session"))
                out[current] = frame

        for row in self._conn.execute(sql, args):
            symbol = row["symbol"]
            if symbol != current:
                _flush()
                buffer, current = [], symbol
            if symbol in wanted:
                buffer.append({"session": row["session"], "close": row["close"],
                               "high": row["high"], "low": row["low"], "volume": row["volume"]})
        _flush()
        return out

    # -- writes ----------------------------------------------------------------

    def upsert(self, symbol: str, frame: pd.DataFrame,
               max_session: str | None = None) -> int:
        """Insert or update bars for one symbol. Returns the row count written.

        A bar with no close is dropped, not stored as NULL: `close` is the only
        field the RS math cannot work around, so a row without one is not a bar.

        `max_session` refuses anything newer than the given ISO session. Callers
        use it to keep the IN-PROGRESS session out of the cache, and it is not
        optional hygiene: Alpaca serves a partial bar for a live session, so
        storing it means the next refresh compares that partial close against a
        later partial close of the same day, sees a move larger than the split
        tolerance, and "repairs" a symbol that never had a corporate action. A
        single morning of that churns a large slice of the universe.
        """
        if frame is None or frame.empty or "close" not in frame.columns:
            return 0

        payload: list[tuple] = []
        for ts, row in frame.iterrows():
            session = _session_key(ts)
            close = row.get("close")
            if session is None or close is None or not _finite(close):
                continue
            if max_session and session > max_session:
                continue
            payload.append((symbol, session, float(close),
                            _opt(row.get("high")), _opt(row.get("low")), _opt(row.get("volume"))))
        if not payload:
            return 0

        self._conn.executemany(
            "INSERT INTO bars (symbol, session, close, high, low, volume) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, session) DO UPDATE SET "
            "  close = excluded.close, high = excluded.high, "
            "  low = excluded.low, volume = excluded.volume", payload)
        self._touch_meta(symbol)
        self._conn.commit()
        return len(payload)

    def _touch_meta(self, symbol: str) -> None:
        row = self._conn.execute(
            "SELECT MIN(session) AS lo, MAX(session) AS hi FROM bars WHERE symbol = ?",
            (symbol,)).fetchone()
        self._conn.execute(
            "INSERT INTO symbol_meta (symbol, adjustment, feed, first_session, "
            "                         last_session, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  adjustment = excluded.adjustment, feed = excluded.feed, "
            "  first_session = excluded.first_session, "
            "  last_session = excluded.last_session, "
            "  refreshed_at = excluded.refreshed_at",
            (symbol, self.adjustment, self.feed, row["lo"], row["hi"], time.time()))

    def mark_empty(self, symbol: str) -> None:
        """Record that we asked for this symbol and the vendor had nothing.

        Without this a symbol with no IEX bars — ADRs and thin foreign listings,
        roughly 1% of the tradable list — has no symbol_meta row, so it looks
        like a brand-new listing on every run and gets the full backfill window
        re-requested forever. The bars never arrive; only the request bill does.
        """
        self._conn.execute(
            "INSERT INTO symbol_meta (symbol, adjustment, feed, refreshed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET refreshed_at = excluded.refreshed_at",
            (symbol, self.adjustment, self.feed, time.time()))
        self._conn.commit()

    def recently_empty(self, within_sec: float = 20 * 3600) -> set[str]:
        """Symbols we asked about and got nothing for, recently enough not to
        bother asking again this run. The window is under a day so a real new
        listing still gets picked up on the next scheduled run."""
        cutoff = time.time() - float(within_sec)
        return {r["symbol"] for r in self._conn.execute(
            "SELECT symbol FROM symbol_meta "
            "WHERE last_session IS NULL AND refreshed_at IS NOT NULL AND refreshed_at >= ?",
            (cutoff,))}

    def purge(self, symbol: str) -> None:
        """Drop every bar for one symbol, keeping its repair history."""
        self._conn.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
        self._conn.execute(
            "UPDATE symbol_meta SET first_session = NULL, last_session = NULL WHERE symbol = ?",
            (symbol,))
        self._conn.commit()

    def record_repair(self, symbol: str, ratio: float | None) -> None:
        """Note that this symbol's history was restated, and by roughly what factor.

        The ratio is audit, not arithmetic — it is never applied to a price.
        Clean fractions (0.5, 0.1, 2.0, 3.0) across the repair log mean the
        detector is catching real splits; scattered values mean the tolerance
        is too tight and it is chasing bar revisions.
        """
        self._conn.execute(
            "INSERT INTO symbol_meta (symbol, adjustment, feed, repairs, last_repair_ratio) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  repairs = symbol_meta.repairs + 1, last_repair_ratio = excluded.last_repair_ratio",
            (symbol, self.adjustment, self.feed, ratio))
        self._conn.commit()

    def trim(self, before_session: str) -> int:
        """Drop bars older than `before_session`. Keeps the file flat instead of
        growing without bound — a year of universe history is ~250 MB."""
        cur = self._conn.execute("DELETE FROM bars WHERE session < ?", (before_session,))
        self._conn.commit()
        return cur.rowcount or 0

    # -- ratings ---------------------------------------------------------------

    def write_ratings(self, as_of: str, rows: list[dict], rs_form: str, population: int) -> int:
        """Persist one session's ratings for EVERY ranked symbol, including the
        ones the day-trading filters drop.

        This is what makes the percentile auditable: /api/rs/check can answer
        "what did NVDA rate?" for a name that never appeared in the served list,
        and it accumulates RS history at no extra cost.
        """
        payload = [(as_of, r.get("ticker"), r.get("rs_rating"), r.get("rs_raw"),
                    rs_form, int(population))
                   for r in rows if r.get("ticker")]
        if not payload:
            return 0
        self._conn.executemany(
            "INSERT INTO ratings (as_of, symbol, rs_rating, rs_raw, rs_form, population) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(as_of, symbol) DO UPDATE SET "
            "  rs_rating = excluded.rs_rating, rs_raw = excluded.rs_raw, "
            "  rs_form = excluded.rs_form, population = excluded.population", payload)
        self._conn.commit()
        return len(payload)

    def rating_history(self, symbol: str, limit: int = 30) -> list[dict]:
        """Most recent ratings for one symbol, newest first."""
        return [dict(r) for r in self._conn.execute(
            "SELECT as_of, rs_rating, rs_raw, rs_form, population FROM ratings "
            "WHERE symbol = ? ORDER BY as_of DESC LIMIT ?", (symbol, int(limit)))]

    def stats(self) -> dict:
        def _one(sql: str):
            row = self._conn.execute(sql).fetchone()
            return row[0] if row else None
        return {
            "symbols": _one("SELECT COUNT(*) FROM symbol_meta"),
            "bars": _one("SELECT COUNT(*) FROM bars"),
            "first_session": _one("SELECT MIN(session) FROM bars"),
            "last_session": _one("SELECT MAX(session) FROM bars"),
            "repairs": _one("SELECT COALESCE(SUM(repairs), 0) FROM symbol_meta"),
            "path": str(self.path),
        }


# ── Small helpers ─────────────────────────────────────────────────────────────

def _finite(value) -> bool:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _opt(value) -> float | None:
    return float(value) if _finite(value) else None


def _session_index(sessions) -> pd.DatetimeIndex:
    """ISO session strings → a DatetimeIndex anchored at ET midnight.

    Anchoring in ET rather than UTC is not cosmetic. Rebuilt as midnight UTC, an
    ET session of 2026-07-24 converts back to 2026-07-23 (UTC-4 in summer), so
    every session date would shift a day on the way out of the cache and `as_of`
    would name the wrong tape. Midnight ET always exists — US DST transitions
    happen at 02:00 — so localising is unambiguous.
    """
    return pd.DatetimeIndex(pd.to_datetime(sessions)).tz_localize(ET)


def _session_key(ts) -> str | None:
    """The ET session date for a bar timestamp, as 'YYYY-MM-DD'.

    Alpaca stamps daily bars at 04:00 UTC (EDT) or 05:00 UTC (EST). Taking the
    UTC date would file every EDT bar under the following day for half the year,
    which shifts anchors and breaks the same-session rule the whole calculation
    rests on.
    """
    try:
        stamp = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if stamp is pd.NaT:
        return None
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.tz_convert("America/New_York").date().isoformat()
