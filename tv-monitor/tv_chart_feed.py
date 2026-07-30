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
from tv_core import (Trail, line_series, read_check, read_heart,  # noqa: E402
                     read_star, series_direction, series_shape,
                     y_to_value)

# How far back a slope looks. Short enough to feel live on a 1m chart, long
# enough that one noisy frame cannot flip an arrow on its own.
SLOPE_WINDOW = 45.0

# Movement over SLOPE_WINDOW below which an indicator counts as flat. Each is
# in its own units: CM RSI-2 and %R are 0..100 scales, the MACD gap is a
# percentage of its panel height.
FLAT_R = 2.0
FLAT_PCT = 2.0
FLAT_M = 0.6

# How much of the plotted line to read back for the trend, in chart columns.
# Columns rather than bars because bar width moves with zoom; ~160px covers a
# meaningful recent stretch at normal zoom without reaching back so far that a
# turn gets averaged away.
TREND_SPAN = 160
TREND_POINTS = 40

# Flat bands for the plot-history trend. Deliberately wider than the tick
# bands above: those measure 45 seconds, these measure ~40 bars, and over that
# much chart an indicator that moved two points has not really done anything.
# Same units as their live counterparts.
FLAT_R_HIST = 6.0
FLAT_PCT_HIST = 6.0
FLAT_M_HIST = 1.5

# Re-locating costs a tesseract pass, so it is not done every poll — but it has
# to happen often enough to survive a window resize or a layout change.
RELOCATE_EVERY = 30.0

# A read older than this is not shown as current.
STALE_AFTER = 10.0

UP, DOWN, FLAT = "up", "down", "flat"
TURNING_UP, TURNING_DOWN = "turning_up", "turning_down"

# ↗ and ↘ are the ones to look for: the line has changed its mind. %R turning
# up off the floor is the entry cue the strategy is built around, and MACD's
# gap rolling over is the warning that a move is done.
_ARROW = {UP: "↑", DOWN: "↓", FLAT: "→",
          TURNING_UP: "↗", TURNING_DOWN: "↘"}

# A turn counts as heading that way for leg purposes — it IS the direction of
# travel, and catching it is the whole point of watching the turn.
_RISING = (UP, TURNING_UP)


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


# How much each indicator counts toward the combined arrow. %R Trend Exhaustion
# leads: it is the one the strategy is actually built around — deep, turning up
# off the floor — while MACD is confirmation that the turn has follow-through.
# The pair sums to 2.0 so the verdict thresholds below keep the meaning they
# were tuned with; shifting the split re-weights without rescaling anything.
PCT_WEIGHT = 1.3
MACD_WEIGHT = 2.0 - PCT_WEIGHT

# Combined verdict → (glyph, rich style, label). %R and MACD each contribute
# ±1.0 to ±2.5 through shape_score, weighted above, so the sum runs -5..+5.
TREND_STYLES = {
    "surging": ("⇈", "bold green",   "up"),
    "rising":  ("↗", "green",        "up"),
    "mixed":   ("→", "yellow",       "mixed"),
    "falling": ("↘", "red",          "down"),
    "sinking": ("⇊", "bold red",     "down"),
    "unknown": ("·", "dim",          "—"),
}


def shape_score(shape: str | None, delta: float | None,
                flat_band: float) -> float:
    """One indicator's contribution: which way, weighted by how decisively.

    Direction alone treats a %R that crawled seven points the same as one that
    ran forty, which is the difference between drift and a real move. Strength
    is measured in flat-bands rather than raw units so %R and MACD — different
    scales entirely — can be compared and summed: 1.0 is just past flat, 2.0 is
    twice the band or more.

    A turn adds half a band on top. It stays a bonus rather than a multiplier
    because a decisive turn and a decisive trend deserve similar weight, and
    the turn is already the harder thing to see.
    """
    if shape in (TURNING_UP, UP):
        sign = 1.0
    elif shape in (TURNING_DOWN, DOWN):
        sign = -1.0
    else:
        return 0.0
    strength = abs(delta or 0.0) / max(1e-9, flat_band)
    weight = min(2.0, max(1.0, strength))
    turn = 0.5 if shape in (TURNING_UP, TURNING_DOWN) else 0.0
    return sign * (weight + turn)


def trend_verdict(pct_shape: str | None, macd_shape: str | None,
                  pct_delta: float | None = None,
                  macd_delta: float | None = None) -> str:
    """Where %R and MACD together say this is heading.

    Only these two. CM RSI-2 at length 2 whips between extremes bar to bar —
    useful as a level ("is it low"), close to meaningless as a direction, so
    letting it vote would mostly add noise. %R Trend Exhaustion and MACD are
    the two that actually trend.

    Agreement is what earns the strong glyphs: both heading the same way is a
    different statement from one moving while the other sits still, and
    outright disagreement is worth showing as disagreement rather than
    averaging into a direction neither indicator supports.
    """
    if pct_shape is None and macd_shape is None:
        return "unknown"

    pct = shape_score(pct_shape, pct_delta, FLAT_PCT_HIST) * PCT_WEIGHT
    macd = shape_score(macd_shape, macd_delta, FLAT_M_HIST) * MACD_WEIGHT

    # Opposing signs are a conflict at any magnitude. A strong %R against a
    # weak MACD is still the two indicators saying different things, and
    # letting the louder one win would report a direction the chart does not
    # agree on. Magnitude only earns weight once they point the same way.
    if pct * macd < 0:
        return "mixed"

    # Both must be moving to earn a doubled glyph, whatever the weights say.
    # ⇈ means the chart agrees with itself; without this, a heavily weighted
    # %R could reach it alone and the glyph would stop meaning agreement.
    both_moving = pct != 0.0 and macd != 0.0
    score = pct + macd
    if score >= 3.0 and both_moving:
        return "surging"
    if score >= 1.0:
        return "rising"
    if score <= -3.0 and both_moving:
        return "sinking"
    if score <= -1.0:
        return "falling"
    return "mixed"


class ChartFeed:
    """Polls one TradingView window and reports indicator direction."""

    def __init__(self,
                 slope_window: float = SLOPE_WINDOW,
                 relocate_every: float = RELOCATE_EVERY,
                 trend_span: int = TREND_SPAN,
                 trend_points: int = TREND_POINTS):
        self.slope_window = float(slope_window)
        self.relocate_every = float(relocate_every)
        self.trend_span = int(trend_span)
        self.trend_points = int(trend_points)

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

        # Gate before spending anything. find_tv_windows costs ~1ms and lists
        # every browser window; a capture is ~33ms and the tesseract axis pass
        # ~200ms. The window TITLE tracks the active tab, so a browser sitting
        # on some other page is rejected here rather than after a third of a
        # second of work that could only ever fail. Minimised and hidden
        # windows never reach this — Quartz is queried on-screen-only — which
        # matters because a window the compositor has stopped drawing would
        # otherwise hand back a stale frame that still parses.
        charts = [(r, t) for r, t in wins
                  if tv_capture_mac.looks_like_chart(t)
                  or "tradingview" in (t or "").lower()]
        if not charts:
            self.last_error = "chart tab not in front"
            return False

        rect, title = charts[0]
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
            img = np.asarray(self.cap.grab(panels["star"]))
            s = read_star(img)
            if s:
                out["r"] = s.get("value")
                # Trend straight off the plot: the line's own history is in
                # the panel, so this is right on the first frame instead of
                # after 45s of sampling. Star's line is grey with red/green
                # only at the extremes, so all three are tried.
                out["r_hist"] = self._panel_series(img, ("gray", "red", "green"),
                                                   100.0, 0.0)
        if "heart" in panels:
            img = np.asarray(self.cap.grab(panels["heart"]))
            h = read_heart(img)
            if h:
                out["pct_w"], out["pct_b"] = h.get("w"), h.get("b")
                out["shade"] = h.get("shade")
                out["pct_w_hist"] = self._panel_series(img, ("white",), 0.0, -100.0)
                out["pct_b_hist"] = self._panel_series(img, ("blue",), 0.0, -100.0)
        # The third slot is keyed "fire" by the locator — that name is left
        # over from the LazyBear squeeze panel this layout no longer carries.
        # It is MACD here, so it is read as MACD.
        macd_key = "check" if "check" in panels else "fire"
        if macd_key in panels:
            img = np.asarray(self.cap.grab(panels[macd_key]))
            c = read_check(img)
            if c:
                out["m"] = c.get("gap")
                # The gap, not the signal line's height. `m` is signal-minus-MA
                # as a percentage of panel height, so its history has to be the
                # same difference — tracking the signal's absolute position
                # would report "rising" for a line climbing while its MA
                # climbed faster, which is the gap closing.
                sig = self._panel_series(img, ("green", "red"), 100.0, 0.0)
                ma = self._panel_series(img, ("yellow",), 100.0, 0.0)
                out["m_hist"] = [
                    None if (a is None or b is None) else a - b
                    for a, b in zip(sig, ma)
                ] if sig and ma else []
        return out or None

    def _panel_series(self, img, colors: tuple[str, ...],
                      top_val: float, bottom_val: float) -> list:
        """The newest stretch of a plotted line, in indicator units.

        All the line's colours at once. This used to try each and keep the
        longest run, which reads a fragment: the signal line is green where it
        rises and red where it falls, so the "best" colour is whichever
        direction dominated the span — and the other half, including any recent
        turn, was dropped.
        """
        h = img.shape[0]
        got = line_series(img, colors, span=self.trend_span,
                          points=self.trend_points)
        return [None if v is None else y_to_value(v, h, top_val, bottom_val)
                for v in got]

    # ── reporting ───────────────────────────────────────────────────────────

    def fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.last_ok > 0 and (now - self.last_ok) <= STALE_AFTER

    def directions(self) -> dict:
        """Per-indicator up/down/flat, or None when it could not be measured.

        Prefers the shape of the plotted line itself — that history is on the
        chart already, so it is right on the first frame and after a restart.
        Falls back to the wall-clock sampler for any panel whose line was too
        broken to read back, which is the only case the Trail still serves.
        """
        v, w = self._values, self.slope_window
        out = {}
        for key, trail, band, hist_band in (
                ("r", self.star, FLAT_R, FLAT_R_HIST),
                ("pct_w", self.heart_w, FLAT_PCT, FLAT_PCT_HIST),
                ("pct_b", self.heart_b, FLAT_PCT, FLAT_PCT_HIST),
                ("m", self.macd, FLAT_M, FLAT_M_HIST)):
            hist = v.get(f"{key}_hist") or []
            shape, delta = series_shape(hist, hist_band)
            out[key] = shape if shape is not None else direction(trail.slope(w), band)
            # The move that `shape` describes — whole span for a trend, second
            # half for a turn — so magnitude and direction never disagree.
            out[f"{key}_delta"] = delta
        return out

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

        # Every leg must have been READ before any of them can be counted. A
        # panel that failed is not the same as an indicator that is not lit,
        # but a leg count cannot express the difference — "1/3" would quietly
        # assert two indicators are unfavourable when one of them was simply
        # never seen. Report which panel is missing and let the circle go dim.
        missing = [name for name, val in
                   (("RSI", r), ("%R", w_ if w_ is not None else b_), ("MACD", m))
                   if val is None]
        if missing:
            self.last_error = f"{'/'.join(missing)} panel unreadable"
            return None

        cm_ok = r is not None and r < 40.0 and d["r"] in _RISING
        pctr_ok = ((w_ is not None and d["pct_w"] in _RISING)
                   or (b_ is not None and d["pct_b"] in _RISING))
        macd_ok = m is not None and (m > 0 or d["m"] in _RISING)

        legs = int(cm_ok) + int(pctr_ok) + int(macd_ok)
        pct = round(legs / 3 * 100)
        return {
            "strategy": "three_indicator",
            "source": "chart",
            "bars_fetched": True,
            "cm_rsi": r, "cm_ok": cm_ok, "cm_rsi_rising": d["r"] in _RISING,
            "pctr": w_, "pctr_slow": b_, "pctr_ok": pctr_ok,
            "macd_ok": macd_ok,
            "proximity_pct": pct,
            "status": ("buy_zone" if pct >= 100 else
                       "aligning" if pct >= 67 else "watching"),
            "in_position": False,
            # Direction of travel from %R and MACD, for the corner indicator.
            # Separate from the leg count on purpose: the legs answer "is the
            # setup complete", this answers "which way is it going", and a
            # setup can be 1-of-3 and clearly building or 2-of-3 and rolling
            # over. Carried alongside rather than folded in, so neither
            # question has to be answered with the other's evidence.
            "chart_trend": trend_verdict(
                d.get("pct_w") or d.get("pct_b"), d.get("m"),
                d.get("pct_w_delta") if d.get("pct_w") else d.get("pct_b_delta"),
                d.get("m_delta")),
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
