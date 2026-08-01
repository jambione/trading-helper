"""Unicode sparklines for the momentum desk.

Pure and dependency-free: give it numbers, get back blocks. No config
lookups, no rich markup, no I/O — the caller owns colour and padding.

`Chg%` is a scalar and hides shape: building, spiking and fading all look
identical at +12%. A short spark makes them distinguishable at a glance.

Two refusals are deliberate, and both exist because a picture of noise reads
as a picture of movement:

  * Too few samples renders "" rather than a couple of blocks. Three blocks
    over six seconds look exactly like three blocks over four minutes.
  * A window whose whole range is a rounding error renders FLAT, not a
    dramatic zigzag. Scaling per row means min->max fills the full height
    whatever the range, so a stock ticking 10.00/10.01 would otherwise draw
    the same violent shape as one moving 30%.
"""
from __future__ import annotations

import math

# Ascending block heights. Index 0 is the floor, -1 the ceiling.
BLOCKS = "▁▂▃▄▅▆▇█"

# Mid height for a flat window — visibly present, visibly going nowhere.
FLAT = BLOCKS[len(BLOCKS) // 2 - 1]


def _clean(values) -> list[float]:
    out = []
    for v in values or ():
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def sparkline(values, width: int = 20, *, min_samples: int = 2,
              flat_pct: float = 0.0) -> str:
    """Blocks for `values`, oldest -> newest. "" when there is nothing to say.

    `width` caps the output by keeping the most recent `width` samples. They
    are not averaged: a spike in the window must survive into the picture.

    `min_samples` is the floor below which no spark is drawn at all.

    `flat_pct` is the smallest peak-to-trough move, as a percentage of the
    window's midpoint, that is allowed to draw shape. Below it the window is
    rendered flat. 0.0 disables the check and restores pure min->max scaling.

    Scaling is per call — the window's own min to its own max — so the spark
    describes shape only. Magnitude belongs to Chg% and RVOL.
    """
    vals = _clean(values)
    if len(vals) < max(2, int(min_samples)):
        return ""
    if width and width > 0:
        vals = vals[-int(width):]

    lo, hi = min(vals), max(vals)
    rng = hi - lo
    if rng <= 0:
        return FLAT * len(vals)

    if flat_pct and flat_pct > 0:
        mid = (hi + lo) / 2.0
        if mid and abs(rng / mid) * 100.0 < float(flat_pct):
            return FLAT * len(vals)

    top = len(BLOCKS) - 1
    return "".join(BLOCKS[min(top, int((v - lo) / rng * top + 0.5))]
                   for v in vals)


def direction(values, min_samples: int = 2) -> int:
    """Net direction across the window: 1 up, -1 down, 0 flat/unknown.

    Compares the first and last retained samples, which is what the spark
    itself depicts — so the colour cannot disagree with the picture.
    """
    vals = _clean(values)
    if len(vals) < max(2, int(min_samples)):
        return 0
    if vals[-1] > vals[0]:
        return 1
    if vals[-1] < vals[0]:
        return -1
    return 0
