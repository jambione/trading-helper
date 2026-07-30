"""
tv_chart_feed.py — the chart's three indicators, as direction rather than digits.

Reads CM RSI-2, %R Trend Exhaustion and MACD straight off the TradingView
window and reports where each one is *heading*. Absolute values are published
too, but they are the supporting detail: a colour-mask read of a plotted line
carries maybe a point or two of noise, so "−65 and rising" is trustworthy in a
way "−64.5" is not, and rising is the part that decides a trade.

Why not the engine: it computes the same three indicators from Alpaca IEX bars,
roughly 2-3% of consolidated volume, on a symbol set it chose for itself. This
reads the pixels already on screen, for whatever is charted, and agrees with
what the user sees by construction rather than by tuning.

Feeds two consumers from one poll:
  • readout()   — the R / % / M lines with direction arrows
  • proximity() — the same reading shaped like signal_proximity, so
                  momentum_signal.buy_circle() consumes it unchanged

Deliberately owns no rendering and no threads. The caller decides how often to
poll and what to do with a stale read.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import tv_capture_mac                                       # noqa: E402
import tv_signal                                            # noqa: E402
from tv_core import Trail, read_check, read_heart, read_star  # noqa: E402

# How far back a slope looks. Short enough to feel live on a 1m chart, long
# enough that one noisy frame cannot flip an arrow on its own.
SLOPE_WINDOW = 45.0

# Movement over SLOPE_WINDOW below which an indicator counts as flat. Each is
# in its own units: CM RSI-2 and %R are 0..100 scales, the MACD gap is a
# percentage of its panel height.
FLAT_R = 2.0
FLAT_PCT = 2.0
FLAT_M = 0.6

# Re-locating costs a tesseract pass, so it is not done every poll — but it has
# to happen often enough to survive a window resize or a layout change.
RELOCATE_EVERY = 30.0

# A read older than this is not shown as current.
STALE_AFTER = 10.0

UP, DOWN, FLAT = "up", "down", "flat"
_ARROW = {UP: "↑", DOWN: "↓", FLAT: "→"}


def direction(delta: float | None, flat_band: float) -> str | None:
    """Slope → up | down | flat, or None while history is too short.

    None is not flat. Flat means measured and not moving; None means we have
    not watched long enough to say, and the caller must not draw an arrow for
    a trend it has not seen.
    """
    if delta is None:
        return None
    if delta > flat_band:
        return UP
    if delta < -flat_band:
        return DOWN
    return FLAT


def arrow(dir_: str | None) -> str:
    return _ARROW.get(dir_ or "", "·")


class ChartFeed:
    """Polls one TradingView window and reports indicator direction."""

    def __init__(self,
                 slope_window: float = SLOPE_WINDOW,
                 relocate_every: float = RELOCATE_EVERY):
        self.slope_window = float(slope_window)
        self.relocate_every = float(relocate_every)

        self.cap = tv_capture_mac.WindowCapture()
        self.rect: dict | None = None
        self.symbol: str | None = None
        self.panels: dict | None = None

        self.star = Trail()
        self.heart_w = Trail()
        self.heart_b = Trail()
        self.macd = Trail()

        self.last_ok = 0.0
        self.last_error: str | None = None
        self._located_at = 0.0
        self._values: dict = {}

    # ── polling ─────────────────────────────────────────────────────────────

    def poll(self, now: float | None = None) -> bool:
        """One capture → read → trail update. True when values were read."""
        now = time.time() if now is None else now

        wins = tv_signal.find_tv_windows()
        if not wins:
            self.last_error = "no TradingView window"
            self.panels = None
            return False

        rect, title = wins[0]
        moved = self.rect != rect
        self.rect = rect
        self.symbol = _symbol_from_title(title) or self.symbol
        self.cap.set_target(rect)

        if moved or self.panels is None or now - self._located_at >= self.relocate_every:
            frame = self.cap.frame()
            if frame is None:
                self.last_error = self.cap.last_error or "capture failed"
                return False
            located = tv_signal.locate_tv_panels(frame, rect)
            # Keep the previous panels on a failed re-locate: a single frame
            # with an obscured axis should not blind an otherwise good feed.
            # A window that actually moved gets no such grace.
            if located:
                self.panels = located
                self._located_at = now
            elif moved:
                self.panels = None
            if not self.panels:
                self.last_error = "could not locate indicator panels"
                return False

        vals = self._read(self.panels)
        if vals is None:
            self.last_error = "panels located but unreadable"
            return False

        self.star.add(vals.get("r"), now)
        self.heart_w.add(vals.get("pct_w"), now)
        self.heart_b.add(vals.get("pct_b"), now)
        self.macd.add(vals.get("m"), now)
        self._values = vals
        self.last_ok = now
        self.last_error = None
        return True

    def _read(self, panels: dict) -> dict | None:
        out: dict = {}
        if "star" in panels:
            s = read_star(np.asarray(self.cap.grab(panels["star"])))
            if s:
                out["r"] = s.get("value")
        if "heart" in panels:
            h = read_heart(np.asarray(self.cap.grab(panels["heart"])))
            if h:
                out["pct_w"], out["pct_b"] = h.get("w"), h.get("b")
                out["shade"] = h.get("shade")
        # The third slot is keyed "fire" by the locator — that name is left
        # over from the LazyBear squeeze panel this layout no longer carries.
        # It is MACD here, so it is read as MACD.
        macd_key = "check" if "check" in panels else "fire"
        if macd_key in panels:
            c = read_check(np.asarray(self.cap.grab(panels[macd_key])))
            if c:
                out["m"] = c.get("gap")
        return out or None

    # ── reporting ───────────────────────────────────────────────────────────

    def fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.last_ok > 0 and (now - self.last_ok) <= STALE_AFTER

    def directions(self) -> dict:
        """Per-indicator up/down/flat, or None where history is too short."""
        w = self.slope_window
        return {
            "r": direction(self.star.slope(w), FLAT_R),
            "pct_w": direction(self.heart_w.slope(w), FLAT_PCT),
            "pct_b": direction(self.heart_b.slope(w), FLAT_PCT),
            "m": direction(self.macd.slope(w), FLAT_M),
        }

    def readout(self, now: float | None = None) -> list[str] | None:
        """The R / % / M lines, values with direction arrows.

        Returns None rather than stale numbers when the feed has gone quiet —
        an indicator readout that silently stops updating is worse than one
        that admits it stopped.
        """
        if not self.fresh(now) or not self._values:
            return None
        v, d = self._values, self.directions()

        def fmt(val, key, digits=0):
            if val is None:
                return "—"
            return f"{val:.{digits}f} {arrow(d.get(key))}"

        pct = "—"
        if v.get("pct_w") is not None or v.get("pct_b") is not None:
            pct = f"{fmt(v.get('pct_w'), 'pct_w')}, {fmt(v.get('pct_b'), 'pct_b')}"
        return [f"R - {fmt(v.get('r'), 'r')}",
                f"% - {pct}",
                f"M - {fmt(v.get('m'), 'm', 1)}"]

    def proximity(self, now: float | None = None) -> dict | None:
        """The reading shaped like the engine's signal_proximity.

        Lets momentum_signal.buy_circle() treat the chart as just another
        source — same three legs, same counting, same four states, no changes
        to code that is already tested.

        The legs are direction-first, matching how the strategy is actually
        read: CM RSI-2 low *and turning up*, %R climbing off the floor, MACD
        above its average or closing on it. A level alone says where price has
        been; the turn is what says it is going somewhere.
        """
        if not self.fresh(now) or not self._values:
            return None
        v, d = self._values, self.directions()
        r, m = v.get("r"), v.get("m")
        w_, b_ = v.get("pct_w"), v.get("pct_b")

        cm_ok = r is not None and r < 40.0 and d["r"] == UP
        pctr_ok = ((w_ is not None and d["pct_w"] == UP)
                   or (b_ is not None and d["pct_b"] == UP))
        macd_ok = m is not None and (m > 0 or d["m"] == UP)

        legs = int(cm_ok) + int(pctr_ok) + int(macd_ok)
        pct = round(legs / 3 * 100)
        return {
            "strategy": "three_indicator",
            "source": "chart",
            "bars_fetched": True,
            "cm_rsi": r, "cm_ok": cm_ok, "cm_rsi_rising": d["r"] == UP,
            "pctr": w_, "pctr_slow": b_, "pctr_ok": pctr_ok,
            "macd_ok": macd_ok,
            "proximity_pct": pct,
            "status": ("buy_zone" if pct >= 100 else
                       "aligning" if pct >= 67 else "watching"),
            "in_position": False,
        }


def _symbol_from_title(title: str) -> str | None:
    """'STKH 2.71 ▼ −24.72% Unnamed' -> 'STKH'.

    The window title is the symbol source: the OS hands it over exactly, so
    none of the legend OCR the Windows build needed applies here.
    """
    parts = (title or "").split()
    if not parts:
        return None
    sym = parts[0].upper()
    return sym if 1 <= len(sym) <= 6 and sym.isalpha() else None


def _selftest(seconds: float = 12.0, interval: float = 1.0) -> int:
    feed = ChartFeed()
    print(f"polling {seconds:.0f}s at {interval:.0f}s intervals…\n")
    t_end = time.time() + seconds
    while time.time() < t_end:
        ok = feed.poll()
        if not ok:
            print(f"  miss: {feed.last_error}")
        else:
            lines = feed.readout() or []
            prox = feed.proximity() or {}
            print(f"  {feed.symbol or '—':<6} " + "   ".join(lines)
                  + f"   -> {prox.get('proximity_pct')}% {prox.get('status')}")
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
