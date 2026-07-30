"""TradingView indicator reading + verdict logic. Importable and testable.

Panels (screen regions calibrated once with tv_calibrate.py):
  star  - 0..100 oscillator. Immediate timing: rising / red = buy-now,
          falling / green = sell-now. Superseded by the longer indicators.
  heart - 0..-100 exhaustion, two lines (white + blue). Both near -100 =
          prime buy zone. Rising toward 0 with red shading = bullish
          (money being made). Falling toward -100 with blue shading =
          bearish. Lines converging = transition.
  check - MACD: signal line vs MA line. Wider gap = stronger call,
          sign gives direction. Optional - dropped from the layout.
  fire  - LazyBear Squeeze Momentum: histogram = momentum (fills the
          strength slot when there is no MACD panel), zero-line
          crosses = squeeze state (black on / gray off = fired).
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
        # Anti-aliasing is why this is not simply "> 170 everywhere". The %R
        # white line is 1px on a steep slope, so its brightness spreads across
        # neighbouring pixels and the newest columns peak around 140-160 —
        # measured on a live chart, where the last 19 columns held two white
        # pixels and line_y needs three. The line read as absent precisely at
        # the right-hand edge, which is the only part anyone cares about.
        #
        # Dropping the threshold alone would start matching gridlines, so
        # neutrality carries the weight instead: white is grey, and the blue
        # and red plots are not. Gridlines here measure ~60-90 and stay out on
        # brightness; blue is ~(220,60,40) and stays out on spread.
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        return (mn > 120) & (mx - mn < 45)
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
    newest data column). None if the color isn't present there.

    Presence is judged on pixel COUNT, not on how many rows the colour
    spans. Requiring two distinct rows quietly rejected any line that
    happened to be flat — it occupies exactly one row — so a MACD signal
    resting on its average, or %R pinned at the floor, read as "no data"
    at precisely the moments those readings matter most. A flat line still
    paints most of the strip's width; a stray anti-aliased pixel does not.
    """
    if x_end is None:
        x_end = img.shape[1] - 4
    strip = img[:, max(0, x_end - edge):x_end + 1]
    mask = color_mask(strip, color)
    if int(mask.sum()) < 3:
        return None
    rows = np.where(mask.any(axis=1))[0]
    if len(rows) == 0:
        return None
    return float(np.median(rows))


def line_series(img: np.ndarray, color: str, x_end: int | None = None,
                span: int = 160, points: int = 40) -> list[float | None]:
    """The plotted line's row at evenly spaced columns, oldest → newest.

    The trend does not have to be accumulated in wall-clock time: the panel
    already holds the line's whole visible history, one column per slice of
    chart. Sampling backwards from the newest column reads that history
    directly, so a slope is available on the first frame instead of after
    45 seconds of watching, it survives a restart, and it measures the shape
    of the plot rather than however the sampler happened to tick.

    `span` is how many columns back to cover — chart columns, not bars, since
    bar width moves with zoom. None entries mark columns where the line was
    not found; callers decide whether enough of it is present to trust.

    The mask is computed once for the whole panel rather than per column,
    which is what makes this affordable at 40 points across three panels.
    """
    if x_end is None:
        x_end = rightmost_data_x(img, (color,))
        if x_end is None:
            return []
    x0 = max(0, x_end - int(span))
    if x_end - x0 < 4:
        return []

    mask = color_mask(img, color)
    xs = np.linspace(x0, x_end, num=max(2, int(points))).astype(int)
    out: list[float | None] = []
    for x in xs:
        # A narrow neighbourhood, so a 1px line with an anti-aliased gap at
        # exactly this column still resolves.
        lo, hi = max(0, x - 2), min(mask.shape[1], x + 3)
        rows = np.where(mask[:, lo:hi].any(axis=1))[0]
        out.append(float(np.median(rows)) if rows.size else None)
    return out


def series_direction(vals: list[float | None],
                     flat_band: float,
                     min_points: int = 6) -> tuple[str | None, float | None]:
    """(direction, change) over a value series, oldest → newest.

    Compares the median of the newest third against the oldest third rather
    than fitting a line: indicator plots are noisy and a median ignores the
    odd column the mask missed, where a least-squares fit would chase it.
    Returns (None, None) when too little of the line was readable — not flat,
    because unread is not the same as unmoving.
    """
    got = [v for v in vals if v is not None]
    if len(got) < min_points:
        return None, None
    third = max(2, len(got) // 3)
    old = float(np.median(got[:third]))
    new = float(np.median(got[-third:]))
    delta = new - old
    if delta > flat_band:
        return "up", delta
    if delta < -flat_band:
        return "down", delta
    return "flat", delta


def series_shape(vals: list[float | None], flat_band: float,
                 min_points: int = 8) -> tuple[str | None, float | None]:
    """Where the line has been AND where it is heading now.

    Returns (shape, delta) where delta is the movement that shape actually
    describes — the whole span for a sustained move, but the SECOND HALF for a
    turn. Those are different numbers and mixing them was wrong: a turn's
    strength is how hard it is turning now, not the net across a span that
    still contains the old direction. Caught live on MACD, which reported shape
    "up" beside a whole-span delta of -1.45, so an upward reading was being
    weighted by the size of a downward move.

    shape is up | down | turning_up | turning_down | flat, or None when
    unreadable.

    A single direction across the whole span hides the thing worth seeing. A
    %R line that fell for thirty bars and turned up over the last eight still
    averages to "down", and that turn off the floor is the entry cue — the
    same for MACD's gap rolling over while its overall slope is still positive.
    So each half is measured separately and disagreement is reported as a turn
    rather than smoothed into the average.

    The half band is deliberately lower than `flat_band`: each half covers
    about half the bars, so holding it to the full-span threshold would call
    real movement flat and never report a turn at all.
    """
    got = [v for v in vals if v is not None]
    if len(got) < min_points:
        return None, None
    mid = len(got) // 2
    half_band = flat_band * 0.6
    first, _ = series_direction(got[:mid], half_band, min_points=3)
    second, second_delta = series_direction(got[mid:], half_band, min_points=3)
    whole, whole_delta = series_direction(got, flat_band)
    if first is None or second is None:
        return whole, whole_delta

    # A turn is described by where it is heading, so its magnitude is the
    # second half's move. A sustained move is described by the whole span.
    if first == "down" and second == "up":
        return "turning_up", second_delta
    if first == "up" and second == "down":
        return "turning_down", second_delta
    if "up" in (first, second) and "down" not in (first, second):
        return "up", whole_delta
    if "down" in (first, second) and "up" not in (first, second):
        return "down", whole_delta
    return "flat", whole_delta


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
    """{'w': white-line val, 'b': blue-line val (0..-100), 'shade': red|blue|none}

    Each line is sampled at its OWN newest column, the way read_check already
    does. Sharing one x_end silently drops whichever line renders shorter — on
    a live chart the white line has been seen ending ~80px before the blue,
    which returned w=None on every read while blue looked perfectly healthy.
    A line that has genuinely stopped is still rejected below.
    """
    h = img.shape[0]
    wx = rightmost_data_x(img, ("white",))
    bx = rightmost_data_x(img, ("blue",))
    if wx is None and bx is None:
        return None

    # Guard against reading a line that stopped rather than one that merely
    # renders a few px shorter: past this much lag it is history, not "now".
    newest = max(x for x in (wx, bx) if x is not None)
    max_lag = max(20, int(img.shape[1] * 0.2))
    if wx is not None and newest - wx > max_lag:
        wx = None
    if bx is not None and newest - bx > max_lag:
        bx = None

    wy = line_y(img, "white", x_end=wx) if wx is not None else None
    by = line_y(img, "blue", x_end=bx) if bx is not None else None
    if wy is None and by is None:
        return None
    x_end = newest
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


def read_squeeze(img: np.ndarray) -> dict | None:
    """LazyBear Squeeze Momentum panel (the fire slot).

    {'mom':      newest histogram bar as % of panel height (+up / -down),
     'building': True = momentum strengthening (lime / bright red bar),
                 False = fading (dark green / maroon),
     'squeeze':  'on' (black zero-line crosses - coiling),
                 'off' (gray crosses), or None if unreadable}

    The axis scale is dynamic (depends on symbol/volatility), so momentum
    is measured as bar length relative to the panel, like the MACD gap.
    """
    h = img.shape[0]
    b, g, r = _split(img)
    grn = g - np.maximum(r, b) > 40    # lime #0f0 and green #080
    red = r - np.maximum(g, b) > 40    # red #f00 and maroon #800
    xs = np.where((grn | red).any(axis=0))[0]
    if not len(xs):
        return None
    x_end = int(xs[-1])

    # zero line = where the bars grow from: green bottoms / red tops
    zeros: list[int] = []
    gcols = grn.any(axis=0)
    if gcols.any():
        zeros += (h - 1 - np.argmax(grn[::-1, :], axis=0))[gcols].tolist()
    rcols = red.any(axis=0)
    if rcols.any():
        zeros += np.argmax(red, axis=0)[rcols].tolist()
    zero_y = float(np.median(zeros))

    # newest bar: direction from dominant family, length from its extent
    strip = slice(max(0, x_end - 5), x_end + 1)
    gpix, rpix = int(grn[:, strip].sum()), int(red[:, strip].sum())
    if gpix >= rpix:
        rows = np.where(grn[:, strip].any(axis=1))[0]
        mom = 100.0 * (zero_y - rows.min()) / h
        chan = g[:, strip][grn[:, strip]]
    else:
        rows = np.where(red[:, strip].any(axis=1))[0]
        mom = -100.0 * (rows.max() - zero_y) / h
        chan = r[:, strip][red[:, strip]]
    building = bool(chan.mean() > 190)   # bright shade = strengthening

    # squeeze state: cross color on the zero line at the newest bars
    y0, y1 = max(0, int(zero_y) - 3), min(h, int(zero_y) + 4)
    band = img[y0:y1, max(0, x_end - 12):x_end + 1].astype(int)
    bb, bg_, br = band[..., 0], band[..., 1], band[..., 2]
    mx = np.maximum(np.maximum(br, bg_), bb)
    mn = np.minimum(np.minimum(br, bg_), bb)
    gray_ct = int(((mn > 90) & (mx < 210) & (mx - mn < 30)).sum())
    black_ct = int((mx < 12).sum())      # true black; dark bg is ~34
    squeeze = None
    if max(gray_ct, black_ct) >= 4:
        squeeze = "off" if gray_ct >= black_ct else "on"
    return {"mom": mom, "building": building, "squeeze": squeeze}


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

    def span(self) -> float:
        """Seconds of history currently held."""
        return self.q[-1][0] - self.q[0][0] if len(self.q) >= 2 else 0.0


def trend5(heart_d: float | None, mom_d: float | None,
           star_d: float | None) -> dict | None:
    """5-minute trend from indicator drifts (each = change over 300s,
    None until that trail has enough history). Heart is the regime
    layer, so it votes double. {'dir': up|down|flat, 'score': -4..4}."""
    if heart_d is None and mom_d is None and star_d is None:
        return None
    score = 0
    if heart_d is not None:
        score += 2 if heart_d > 5 else -2 if heart_d < -5 else 0
    if mom_d is not None:
        score += 1 if mom_d > 2 else -1 if mom_d < -2 else 0
    if star_d is not None:
        score += 1 if star_d > 10 else -1 if star_d < -10 else 0
    return {"dir": ("up" if score >= 2 else
                    "down" if score <= -2 else "flat"),
            "score": score, "heart": heart_d, "mom": mom_d,
            "star": star_d}


# ------------------------------------------------------ grid + leader -------

def grid_cells(w: int, h: int, rows: int, cols: int) -> list[dict]:
    """Equal RxC split of a window into chart cells, reading order
    (left-to-right, top-to-bottom). Cells are window-relative
    {'left','top','width','height'} — the caller anchors them to the
    screen. TradingView multi-chart layouts tile equally by default."""
    cells = []
    ys = [round(h * r / rows) for r in range(rows + 1)]
    xs = [round(w * c / cols) for c in range(cols + 1)]
    for r in range(rows):
        for c in range(cols):
            cells.append({"left": xs[c], "top": ys[r],
                          "width": xs[c + 1] - xs[c],
                          "height": ys[r + 1] - ys[r]})
    return cells


# verdict base for ranking: how loudly combine() is saying "long here".
# Order matters — startswith, longest phrases first.
_VERDICT_BASE = (("STRONG BUY", 5.0), ("LEAN BUY", 3.0), ("BUY", 4.0),
                 ("WATCH", 2.0), ("STRONG SELL", -5.0), ("LEAN SELL", -3.0),
                 ("SELL", -4.0))

FIRED_FRESH = 120.0   # a squeeze fire only tilts the score this long


def bullish_score(verdict: str, trend: dict | None = None,
                  sq: dict | None = None) -> float:
    """How much this chart deserves the focus chart. The combine()
    verdict is the base; the 5m trend and squeeze state tilt it.
    Range roughly -12..+12."""
    score = 0.0
    for prefix, base in _VERDICT_BASE:
        if verdict.startswith(prefix):
            score = base
            break
    if trend:
        score += trend.get("score") or 0
    if sq:
        if sq.get("fired") and (sq.get("fired_ago") or 0) <= FIRED_FRESH:
            score += 2.0 if sq["fired"] == "LONG" else -2.0
        if sq.get("building"):
            mom = sq.get("mom") or 0.0
            score += 1.0 if mom > 0 else -1.0 if mom < 0 else 0.0
    return score


class LeaderTracker:
    """Names the chart that deserves the focus chart. A challenger must
    beat the sitting leader by `margin` for `confirm` consecutive reads —
    flapping between two hot charts is worse than being five seconds
    late. A vacant seat (no leader, leader left the grid, or leader went
    bearish) is filled immediately."""

    def __init__(self, margin: float = 1.5, confirm: int = 5):
        self.margin = margin
        self.confirm = max(1, int(confirm))
        self.leader: str | None = None
        self._challenger: str | None = None
        self._streak = 0

    def update(self, scores: dict[str, float]) -> str | None:
        """scores = {symbol: bullish_score}. Returns the current leader,
        None when nothing on the grid is bullish (score > 0)."""
        bulls = {s: v for s, v in scores.items() if v > 0}
        cur = self.leader
        if cur is None or cur not in scores or scores[cur] <= 0:
            self.leader = max(bulls, key=bulls.get) if bulls else None
            self._challenger, self._streak = None, 0
            return self.leader
        top = max(bulls, key=bulls.get) if bulls else None
        if top and top != cur and bulls[top] >= scores[cur] + self.margin:
            self._streak = self._streak + 1 if top == self._challenger else 1
            self._challenger = top
            if self._streak >= self.confirm:
                self.leader = top
                self._challenger, self._streak = None, 0
        else:
            self._challenger, self._streak = None, 0
        return self.leader


# -------------------------------------------------------------- verdict -----

def master_verdict(tv_verdict: str, l2: dict | None) -> dict | None:
    """Combine the TradingView verdict (the setup) with flow order
    flow (the trigger). l2 = {'bias': -100..100, 'play': playbook verdict}.

    EXECUTE only when both agree; a bullish chart with a selling tape is
    a conflict, not an entry.

    When the L2 monitor reports an open position (l2['pos'], anchored to
    the real broker fill by the position feed), the verdict flips from
    entry-mode to exit-mode: exits act on the FIRST strong contrary
    evidence instead of waiting for everything to align the way entries
    do — by the time chart and tape both confirm, the move has usually
    given most of it back."""
    if not l2:
        return None
    bull_tv = "BUY" in tv_verdict
    bear_tv = "SELL" in tv_verdict
    bias = l2.get("bias", 0)
    play = str(l2.get("play", ""))
    l2_bull = bias >= 25 or play in ("LONG_TRIGGER", "LONG_SETUP")
    l2_bear = bias <= -25 or play == "BEAR_CRACK"

    pos = l2.get("pos")
    if pos:
        up = pos.get("upnl") or 0.0
        anchor = (f"{'real' if pos.get('real') else 'paper'} entry "
                  f"{pos.get('entry')}, uPnL {up:+.2f}%")
        if bear_tv and l2_bear:
            return {"verdict": "EXIT NOW",
                    "why": f"chart and tape both turned — {anchor}"}
        if l2_bear:
            if up > 0:
                return {"verdict": "TAKE PROFIT — tape turned",
                        "why": f"order flow selling while green — {anchor}"}
            return {"verdict": "TIGHTEN — tape selling",
                    "why": f"flow turned against a red position — {anchor}"}
        if bear_tv:
            if up > 0:
                return {"verdict": "TAKE PROFIT — chart turned",
                        "why": f"chart bearish while green — {anchor}"}
            return {"verdict": "TIGHTEN — chart bearish",
                    "why": f"chart turned but tape holding — {anchor}"}
        if bull_tv and l2_bull:
            return {"verdict": "HOLD — trend intact",
                    "why": f"chart and tape still bullish — {anchor}"}
        return {"verdict": "HOLD — no exit signal",
                "why": f"no bearish evidence yet — {anchor}"}

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
            gap_slope: float | None, squeeze: dict | None = None,
            trend: dict | None = None) -> dict:
    """The user's hierarchy: heart = regime, strength = check (MACD) or
    the squeeze momentum when the chart carries no MACD panel,
    star = timing, squeeze state (coiling/fired) = context.

    squeeze = SqueezeTracker.update() output: read_squeeze() fields
    plus 'fired' ('LONG'/'SHORT'/None) and 'fired_ago' seconds.
    trend = trend5() output; an opposing 5m trend demotes STRONG
    verdicts but never blocks the regime layer outright."""
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

    # strength: check (MACD) when present, else the squeeze momentum
    # bar - same class of signal, so it inherits the second-opinion slot
    direction = strength = None
    if check:
        gap = check["gap"]
        direction = "bull" if gap > 0.5 else "bear" if gap < -0.5 else "flat"
        strength = abs(gap)
        widen = (gap_slope or 0) * (1 if gap >= 0 else -1) > 0
        reasons.append(f"MACD: {direction} gap {gap:+.1f}%"
                       + (" widening" if widen else ""))
    elif squeeze:
        mom = squeeze["mom"]
        direction = "bull" if mom > 0.5 else "bear" if mom < -0.5 else "flat"
        strength = abs(mom)
        reasons.append(f"squeeze mom: {direction} {mom:+.1f}% "
                       + ("building" if squeeze["building"] else "fading"))

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
    if squeeze:
        if squeeze.get("fired"):
            reasons.append(f"squeeze FIRED {squeeze['fired']} "
                           f"{squeeze.get('fired_ago', 0)}s ago")
        elif squeeze.get("squeeze") == "on":
            reasons.append("squeeze ON - coiling for a move")

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

    if trend:
        reasons.append(f"5m trend: {trend['dir']}")
        if verdict == "STRONG BUY" and trend["dir"] == "down":
            verdict = "BUY"
            reasons.append("demoted - against the 5m trend")
        elif verdict == "STRONG SELL" and trend["dir"] == "up":
            verdict = "SELL"
            reasons.append("demoted - against the 5m trend")
    return {"verdict": verdict, "regime": regime, "timing": timing,
            "direction": direction, "strength": strength,
            "reasons": reasons}
