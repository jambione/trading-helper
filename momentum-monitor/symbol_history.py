"""Bounded per-symbol sample ring for the momentum desk.

Memory-only. Nothing here persists across a restart and nothing here does
I/O — the render loop pushes one sample per symbol per poll and reads back
short series for the price sparkline and the mention-trend arrow. Fields are
added here only when something actually reads them.

Sizing: at the default poll_interval of 2.0s, 120 samples is ~4 minutes of
tape per symbol. `maxlen` comes from the `history_samples` config key rather
than a module constant so the window can be retuned without a code change.

Every public method is exception-safe on bad input: a garbage sample is
dropped, never raised, because the caller is the render loop.
"""
from __future__ import annotations

import math
from collections import deque

# Sample fields accepted by push(). Anything else is ignored, so a caller
# that starts sending a new field before this list learns about it degrades
# to "no data" instead of raising.
FIELDS = ("price", "mention_window", "mention_velocity")

DEFAULT_MAXLEN = 120


def _num(v) -> float | None:
    """Coerce to float, or None for anything non-numeric (incl. bool)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN/inf would poison min/max scaling in the sparkline.
    return f if math.isfinite(f) else None


class SymbolHistory:
    """Bounded per-symbol sample ring. Memory-only; nothing persists here."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN):
        try:
            self.maxlen = max(1, int(maxlen))
        except (TypeError, ValueError):
            self.maxlen = DEFAULT_MAXLEN
        self._data: dict[str, deque] = {}

    # ── write ────────────────────────────────────────────────────────────
    def push(self, sym: str, ts: float, *, price=None, mention_window=None,
             mention_velocity=None) -> None:
        """Append one sample. Oldest is evicted once the ring is full.

        A sample whose every field is None is still recorded: the timestamp
        alone is evidence the symbol was live at `ts`, and series() skips
        the empty fields anyway.
        """
        key = str(sym or "").upper()
        if not key:
            return
        t = _num(ts)
        if t is None:
            return
        ring = self._data.get(key)
        if ring is None:
            ring = self._data[key] = deque(maxlen=self.maxlen)
        ring.append({
            "ts": t,
            "price": _num(price),
            "mention_window": _num(mention_window),
            "mention_velocity": _num(mention_velocity),
        })

    # ── read ─────────────────────────────────────────────────────────────
    def series(self, sym: str, field: str) -> list[float]:
        """Values for `field`, oldest → newest, with None samples skipped.

        Skipping rather than zero-filling matters: a gap in the price feed
        must not render as a drop to zero on a sparkline.
        """
        ring = self._data.get(str(sym or "").upper())
        if not ring or field not in FIELDS:
            return []
        return [s[field] for s in ring if s.get(field) is not None]

    def count(self, sym: str) -> int:
        ring = self._data.get(str(sym or "").upper())
        return len(ring) if ring else 0

    def symbols(self) -> set[str]:
        return set(self._data)

    # ── lifecycle ────────────────────────────────────────────────────────
    def prune(self, live: set[str]) -> None:
        """Evict symbols no longer on the feed.

        Called once per loop. Without it a long session leaks a ring per
        churned symbol, and the desk runs for hours.
        """
        keep = {str(s or "").upper() for s in (live or ())}
        for sym in [s for s in self._data if s not in keep]:
            del self._data[sym]
