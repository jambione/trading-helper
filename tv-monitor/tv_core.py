"""TradingView indicator reading + verdict logic. Importable and testable.

Panels (screen regions calibrated once with tv_calibrate.py):
  star  - 0..100 oscillator. Immediate timing: rising / red = buy-now,
          falling / green = sell-now. Superseded by the longer indicators.
  heart - 0..-100 exhaustion, two lines (white + blue). Both near -100 =
          prime buy zone. Rising toward 0 with red shading = bullish
          (money being made). Falling toward -100 with blue shading =
          bearish. Lines converging = transition.
  check - MACD: signal line vs MA line. Wider gap = stronger call,
          sign gives direction.
  fire  - custom bull/bear labels (context only).
"""
from __future__ import annotations

import time
from collections import deque

import numpy as np

# ------------------------------------------------------- pixel reading ------
# mss frames are BGRA. Channel order: [B, G, R, A].


def _split(strip: np.ndarray):
    s = strip.astype(int)
    return s[..., 0], s[..., 1], s[..., 2]      # B, G, R


def color_mask(strip: np.ndarray, name: str) -> np.ndarray:
    b, g, r = _split(strip)
    if name == "red":
        return (r > 140) & (g < 110) & (b < 110)
    if name == "green":
        return (g > 140) & (r < 125) & (b < 125)
    if name == "blue":
        return (b > 140) & (b - r > 20) & (g < b)
    if name == "white":
        return (r > 170) & (g > 170) & (b > 170)
    if name == "yellow":
        return (r > 130) & (g > 150) & (b < 130)
    if name == "gray":
        # neutral line colors (TradingView's RSI line is mid-gray, not
        # white): bright-ish on all channels, low color saturation
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        return (mn > 115) & (mx - mn < 30)
    raise ValueError(name)


def rightmost_data_x(img: np.ndarray, colors: tuple) -> int | None:
    """Rightmost column containing any of the given line colors - i.e.
    where the newest plotted bar actually is. TradingView often shows
    empty 'future' space between the last bar and the price axis."""
    m = None
    for c in colors:
        cm = color_mask(img, c)
        m = cm if m is None else (m | cm)
    xs = np.where(m.any(axis=0))[0]
    return int(xs[-1]) if len(xs) else None


def line_y(img: np.ndarray, color: str, edge: int = 14,
           x_end: int | None = None) -> float | None:
    """Median row of `color` pixels in the strip ending at x_end (the
    newest data column). None if the color isn't present there."""
    if x_end is None:
        x_end = img.shape[1] - 4
    strip = img[:, max(0, x_end - edge):x_end + 1]
    rows = np.where(color_mask(strip, color).any(axis=1))[0]
    if len(rows) < 2:
        return None
    return float(np.median(rows))


def y_to_value(y: float, height: int, top_val: float,
               bottom_val: float) -> float:
    return top_val + (y / max(1, height - 1)) * (bottom_val - top_val)


def read_star(img: np.ndarray) -> dict | None:
    """{'value': 0..100, 'color': red|green|none}

    The line itself is gray; red/green segments overlay it at extremes.
    x_end must track the GRAY line's newest column - otherwise a stale
    red/green blob left behind on the chart pins the reading."""
    h = img.shape[0]
    x_end = rightmost_data_x(img, ("gray", "red", "green"))
    if x_end is None:
        return None
    ys = [line_y(img, c, x_end=x_end) for c in ("gray", "red", "green")]
    ys_ok = [y for y in ys if y is not None]
    if not ys_ok:
        return None
    val = y_to_value(min(ys_ok), h, 100.0, 0.0)   # topmost line pixel
    color = "red" if ys[1] is not None else ("green" if ys[2] is not None
                                             else "none")
    return {"value": val, "color": color}


def read_heart(img: np.ndarray) -> dict | None:
    """{'w': white-line val, 'b': blue-line val (0..-100), 'shade': red|blue|none}"""
    h = img.shape[0]
    x_end = rightmost_data_x(img, ("white", "blue"))
    if x_end is None:
        return None
    wy = line_y(img, "white", x_end=x_end)
    by = line_y(img, "blue", x_end=x_end)
    if wy is None and by is None:
        return None
    w = y_to_value(wy, h, 0.0, -100.0) if wy is not None else None
    b = y_to_value(by, h, 0.0, -100.0) if by is not None else None
    # dominant shading near the newest data (fills are dim -> loose test)
    strip = img[:, max(0, x_end - 30):max(1, x_end - 2)].astype(int)
    rmean = strip[..., 2].mean()
    bmean = strip[..., 0].mean()
    shade = ("red" if rmean - bmean > 8 else
             "blue" if bmean - rmean > 8 else "none")
    return {"w": w, "b": b, "shade": shade}


def read_check(img: np.ndarray) -> dict | None:
    """{'gap': signal-above-MA gap as % of panel height (+bull/-bear)}

    On this chart the SIGNAL is the thick line that recolors green (up) /
    red (down); the MA is the yellow line. Lines can end at slightly
    different columns, so each is sampled at its own newest column."""
    h = img.shape[0]
    sig_x = rightmost_data_x(img, ("green", "red"))
    ma_x = rightmost_data_x(img, ("yellow",))
    if sig_x is None or ma_x is None:
        return None
    sig = line_y(img, "green", x_end=sig_x)
    if sig is None:
        sig = line_y(img, "red", x_end=sig_x)
    ma = line_y(img, "yellow", x_end=ma_x)
    if sig is None or ma is None:
        return None
    return {"gap": 100.0 * (ma - sig) / h}   # signal above MA -> positive


# --------------------------------------------------------- state + slope ----

class Trail:
    """Keeps (ts, value) history and reports slope over a lookback."""

    def __init__(self, maxlen: int = 600):
        self.q = deque(maxlen=maxlen)

    def add(self, v: float | None, ts: float | None = None):
        if v is not None:
            self.q.append((ts or time.time(), v))

    def slope(self, seconds: float = 10.0) -> float | None:
        """Change per lookback window (units of the value)."""
        if len(self.q) < 2:
            return None
        t_last, v_last = self.q[-1]
        base = next(((t, v) for t, v in self.q if t >= t_last - seconds),
                    None)
        if base is None or base[0] == t_last:
            return None
        return v_last - base[1]


# -------------------------------------------------------------- verdict -----

def master_verdict(tv_verdict: str, l2: dict | None) -> dict | None:
    """Combine the TradingView verdict (the setup) with Webull L2 order
    flow (the trigger). l2 = {'bias': -100..100, 'play': playbook verdict}.

    EXECUTE only when both agree; a bullish chart with a selling tape is
    a conflict, not an entry."""
    if not l2:
        return None
    bull_tv = "BUY" in tv_verdict
    bear_tv = "SELL" in tv_verdict
    bias = l2.get("bias", 0)
    play = str(l2.get("play", ""))
    l2_bull = bias >= 25 or play in ("LONG_TRIGGER", "LONG_SETUP")
    l2_bear = bias <= -25 or play == "BEAR_CRACK"

    if bull_tv and l2_bull:
        return {"verdict": "EXECUTE BUY",
                "why": "chart setup + live order flow aligned"}
    if bull_tv and l2_bear:
        return {"verdict": "CONFLICT — HOLD",
                "why": "chart bullish but the tape is selling"}
    if bull_tv:
        return {"verdict": "BUY SETUP — awaiting flow",
                "why": "indicators ready; waiting for L2 to confirm"}
    if bear_tv and l2_bear:
        return {"verdict": "EXECUTE SELL",
                "why": "chart and tape both bearish"}
    if bear_tv:
        return {"verdict": "SELL LEANING",
                "why": "chart bearish; flow not confirming yet"}
    if l2_bull or l2_bear:
        side = "buying" if l2_bull else "selling"
        return {"verdict": "WAIT",
                "why": f"tape {side} but no chart setup behind it"}
    return {"verdict": "WAIT", "why": "no combined edge"}

def combine(star: dict | None, heart: dict | None, check: dict | None,
            star_slope: float | None, heart_slope: float | None,
            gap_slope: float | None, fire_label: str | None = None) -> dict:
    """The user's hierarchy: heart = regime, check = strength,
    star = timing, fire = context only."""
    reasons = []

    # regime from heart (exhaustion) - THE deciding layer
    regime = "unknown"
    if heart:
        w, b = heart.get("w"), heart.get("b")
        both = [v for v in (w, b) if v is not None]
        if both and all(v <= -85 for v in both) and len(both) == 2:
            regime = "buy-zone"
            reasons.append("exhaustion: both lines near -100 (prime zone)")
        elif heart["shade"] == "red" and (heart_slope or 0) > 0.5:
            regime = "bullish"
            reasons.append("exhaustion: climbing with red shade "
                           "(money coming in)")
        elif heart["shade"] == "blue" and (heart_slope or 0) < -0.5:
            regime = "bearish"
            reasons.append("exhaustion: falling with blue shade "
                           "(money leaving)")
        else:
            regime = "mixed"
            reasons.append("exhaustion: no clear regime")

    # strength from check (MACD) - second opinion
    direction = strength = None
    if check:
        gap = check["gap"]
        direction = "bull" if gap > 0.5 else "bear" if gap < -0.5 else "flat"
        strength = abs(gap)
        widen = (gap_slope or 0) * (1 if gap >= 0 else -1) > 0
        reasons.append(f"MACD: {direction} gap {gap:+.1f}%"
                       + (" widening" if widen else ""))

    # timing from star (RSI) - lowest weight
    timing = "flat"
    if star:
        if (star_slope or 0) > 2 or star["color"] == "red":
            timing = "buy"
        elif (star_slope or 0) < -2 or star["color"] == "green":
            timing = "sell"
        reasons.append(f"RSI: {star['value']:.0f} "
                       f"{'rising' if (star_slope or 0) > 0 else 'falling'}"
                       + (f", {star['color']}" if star["color"] != "none"
                          else ""))
    if fire_label:
        reasons.append(f"big picture: {fire_label}")

    # verdict hierarchy: exhaustion > MACD > RSI
    if regime == "bullish":
        if direction == "bull":
            verdict = "STRONG BUY"
        else:
            verdict = "BUY"
            if direction == "bear":
                reasons.append("MACD lagging - exhaustion leads, "
                               "expect it to catch up")
        if timing == "sell":
            reasons.append("RSI not confirming yet (lowest weight)")
    elif regime == "buy-zone":
        verdict = ("BUY" if (heart_slope or 0) > 0
                   else "WATCH — exhaustion low, wait for the turn")
    elif regime == "bearish":
        verdict = "STRONG SELL" if direction == "bear" else "SELL"
    else:  # mixed regime -> the lower layers must fully agree
        if direction == "bull" and timing == "buy":
            verdict = "LEAN BUY (no regime backing)"
        elif direction == "bear" and timing == "sell":
            verdict = "LEAN SELL (no regime backing)"
        else:
            verdict = "WAIT"
    return {"verdict": verdict, "regime": regime, "timing": timing,
            "direction": direction, "strength": strength,
            "reasons": reasons}
