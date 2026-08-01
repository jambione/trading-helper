"""Append-only session journal for the momentum desk.

One JSONL file per session date under `momentum-monitor/journal/`. One record
per rising edge — never per poll — so the file is a log of events, not a
sampling of state.

Why it exists: when a symbol leaves the feed it vanishes and nothing records
what happened next. There is no way to know the FOCUS hit rate, or whether
`rsi_focus_max: 35` is a good threshold or a lucky guess. This is the only
part of the roadmap that produces evidence rather than a hypothesis.

Two constraints shape the design.

Writes are buffered and flushed at most once per `flush_sec`, because the
render loop must never block on disk. A failing disk logs once and is
otherwise swallowed; the buffer is bounded so a full volume cannot leak
memory across a session.

Fields whose value is unknown are OMITTED rather than written as null or 0.
This matters because the file is the input to threshold tuning: an `rvol` of
0 that really means "no data" would quietly bias every bucket it lands in,
and `"confluence": []` would assert "no confirming sources" when the truth is
"the server did not tell us". A reader must use .get() and handle absence.
"""
from __future__ import annotations

import contextlib
import json
import math
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Own the timezone outright rather than inheriting it. Falling back to naive
# local time would stamp `et` and the daily filename in whatever zone the host
# happens to be in — correct on an ET desk, silently wrong on any other, and
# invisible either way. zoneinfo is stdlib, so there is no reason to risk it.
ET = ZoneInfo("America/New_York")

# session_clock lives at the repo root; don't depend on the importer having
# arranged sys.path for us.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from session_clock import session_window
except Exception:                                          # noqa: BLE001
    session_window = None

# Cap on unflushed records. Reached only when the disk is failing, in which
# case the oldest are dropped rather than growing without bound.
MAX_BUFFER = 2000


def _num(v):
    """Numeric or None. Bools are not numbers here."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _et_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def journal_record(kind: str, sym: str, row: dict, ts: float, *,
                   st_rank: int | None = None,
                   rvol: float | None = None,
                   extra: dict | None = None) -> dict:
    """Build one journal record. Pure — derives everything from `ts`, never
    from the wall clock, so a buffered record is stamped with when the event
    happened rather than when it reached disk.

    Keys with nothing behind them are left out entirely. See the module
    docstring: absence and zero must stay distinguishable downstream.
    """
    dt = _et_dt(ts)
    rec: dict = {
        "ts": round(float(ts), 3),
        "et": dt.strftime("%H:%M:%S"),
        "kind": str(kind),
        "sym": str(sym or "").upper(),
    }

    sp = row.get("signal_proximity")
    sp = sp if isinstance(sp, dict) else {}

    for key, src in (("price", row.get("price")),
                     # Seconds since the print `price` came from. T4.2 needs
                     # this to exclude events whose entry reference was stale:
                     # a forward return measured from a 60s-old price is
                     # measuring from the wrong place.
                     ("price_age_sec", row.get("price_age_sec")),
                     ("pct_change", row.get("pct_change")),
                     ("mention_window", row.get("mention_window")),
                     ("mention_count", row.get("mention_count")),
                     ("cm_rsi", sp.get("cm_rsi")),
                     ("pctr", sp.get("pctr")),
                     ("pctr_slow", sp.get("pctr_slow")),
                     ("proximity_pct", sp.get("proximity_pct")),
                     ("mention_velocity", sp.get("mention_velocity"))):
        v = _num(src)
        if v is not None:
            rec[key] = v

    v = _num(rvol)
    if v is not None:
        rec["rvol"] = v

    # Only when the server actually published it. An empty list would claim
    # "no confirming sources", which is a different statement from silence.
    conf = row.get("confluence")
    if isinstance(conf, dict):
        srcs = conf.get("sources")
        if isinstance(srcs, list) and srcs:
            rec["confluence"] = [str(s) for s in srcs]
        n = _num(conf.get("count"))
        if n is not None:
            rec["confluence_count"] = int(n)

    if st_rank is not None:
        n = _num(st_rank)
        if n is not None:
            rec["st_rank"] = int(n)

    if session_window is not None:
        with contextlib.suppress(Exception):
            rec["session_window"] = session_window(dt)[0]

    if row.get("find_it_first"):
        rec["find_it_first"] = True
    if row.get("mention_burst"):
        rec["mention_burst"] = True

    if extra:
        for k, v in extra.items():
            if v is not None:
                rec[k] = v
    return rec


class Journal:
    """Buffered append-only JSONL writer. Never raises at the call site."""

    def __init__(self, directory, flush_sec: float = 5.0,
                 enabled: bool = True, log=None):
        self.dir = Path(directory)
        self.flush_sec = max(0.0, float(flush_sec))
        self.enabled = bool(enabled)
        self._buf: deque = deque(maxlen=MAX_BUFFER)
        self._last_flush = 0.0
        self._error_logged = False
        self._log = log
        self.dropped = 0

    # ── write ────────────────────────────────────────────────────────────
    def record(self, kind: str, sym: str, row: dict, ts: float, **kw) -> None:
        if not self.enabled:
            return
        try:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1
            self._buf.append(journal_record(kind, sym, row, ts, **kw))
        except Exception:                                  # noqa: BLE001
            self._note("record failed")

    # ── flush ────────────────────────────────────────────────────────────
    def due(self, now: float) -> bool:
        return bool(self._buf) and (now - self._last_flush) >= self.flush_sec

    def maybe_flush(self, now: float) -> int:
        return self.flush(now) if self.due(now) else 0

    def flush(self, now: float | None = None) -> int:
        """Append the buffer to disk. Returns records written.

        Records are grouped by their own ET date, so a buffer spanning
        midnight lands in the right two files rather than all in one.
        """
        if not self.enabled or not self._buf:
            return 0
        pending = list(self._buf)
        self._buf.clear()
        if now is not None:
            self._last_flush = now

        by_date: dict = {}
        for rec in pending:
            try:
                day = _et_dt(float(rec["ts"])).strftime("%Y-%m-%d")
            except Exception:                              # noqa: BLE001
                day = "unknown"
            by_date.setdefault(day, []).append(rec)

        written = 0
        for day, recs in by_date.items():
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                lines = "".join(
                    json.dumps(r, separators=(",", ":"),
                               default=str) + "\n" for r in recs)
                with open(self.dir / f"{day}.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(lines)
                written += len(recs)
            except Exception as e:                         # noqa: BLE001
                # Disk full, permissions, read-only volume. The desk keeps
                # running; these records are gone.
                self.dropped += len(recs)
                self._note(f"write failed: {e}")
        return written

    def close(self) -> int:
        """Final flush. Called from the Ctrl+C path — the desk is stopped that
        way every day and the last records must not be lost."""
        try:
            return self.flush()
        except Exception:                                  # noqa: BLE001
            self._note("close failed")
            return 0

    # ── internals ────────────────────────────────────────────────────────
    def _note(self, msg: str) -> None:
        if self._error_logged:
            return
        self._error_logged = True
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log(f"[JOURNAL] {msg} — journaling degraded")
