"""Shared building blocks for harness signal files. Everything here is causal: column m uses bars <= m only.

Indicators are warmed with the PRIOR session's bars (prev + day side by side), so a 112-bar line exists at 09:30
instead of 11:22. That is the offline answer to "the slow line needs two hours of bars before the first open"."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NBAR = 390


def _ffill(a):
    """Forward-fill NaN along rows (leading NaN stays NaN)."""
    a = np.asarray(a, dtype=float)
    idx = np.where(~np.isnan(a), np.arange(a.shape[1]), 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    out = a[np.arange(a.shape[0])[:, None], idx]
    out[np.isnan(a[:, [0]]).repeat(a.shape[1], 1) & (idx == 0)] = np.nan
    return out


def joined(day, prev, key):
    """prev session then today, (n, 780). Missing bars carry the last close so windows keep their clock width."""
    c = _ffill(np.concatenate([prev["c"], day["c"]], axis=1))
    if key == "c":
        return c
    x = np.concatenate([prev[key], day[key]], axis=1)
    if key == "v":
        return np.nan_to_num(x)
    return np.where(np.isnan(x), c, x)


def roll(a, n, fn):
    """fn over the trailing n columns INCLUDING the current one; first n-1 columns NaN."""
    out = np.full(a.shape, np.nan)
    with np.errstate(all="ignore"):
        out[:, n - 1:] = fn(sliding_window_view(a, n, axis=1), axis=-1)
    return out


def percent_r(day, prev, length, ewm_span=1):
    """Williams %R (0 = at the high, -100 = at the low) over `length` 1-minute bars, optional EMA smoothing.
    Returns today's (n, 390) slice."""
    h, l, c = joined(day, prev, "h"), joined(day, prev, "l"), joined(day, prev, "c")
    hh, ll = roll(h, length, np.nanmax), roll(l, length, np.nanmin)
    with np.errstate(all="ignore"):
        r = np.where(hh > ll, -100.0 * (hh - c) / (hh - ll), np.nan)
    if ewm_span > 1:
        a = 2.0 / (ewm_span + 1.0)
        s = np.full(r.shape, np.nan)
        acc = np.full(r.shape[0], np.nan)
        for m in range(r.shape[1]):
            x = r[:, m]
            acc = np.where(np.isnan(acc), x, np.where(np.isnan(x), acc, a * x + (1 - a) * acc))
            s[:, m] = acc
        r = s
    out = r[:, NBAR:]
    return np.where(np.isnan(day["c"]), np.nan, out)          # no decision on a minute with no bar


def vol_ratio(day, n=20):
    """This bar's volume over the mean of the previous n bars of TODAY (NaN until n bars exist)."""
    v = np.nan_to_num(day["v"])
    cs = np.concatenate([np.zeros((v.shape[0], 1)), np.cumsum(v, axis=1)], axis=1)
    avg = np.full(v.shape, np.nan)
    avg[:, n:] = (cs[:, n:-1] - cs[:, :-n - 1]) / n
    with np.errstate(all="ignore"):
        return np.where(avg > 0, v / avg, np.nan)


def after(sig, minute):
    """Blank decisions before `minute` bars after 09:30 (5 = 09:35)."""
    sig = sig.copy()
    sig[:, :minute] = False
    return sig


def percent_r_session(day, length, min_bars, ewm_span=1):
    """VARIABLE-length %R on TODAY's bars only: uses however many bars exist so far (at least min_bars), growing
    to `length`. This is the desk knob ai_watch_exhaustion_slow_min_bars, offline."""
    c = _ffill(day["c"])
    n = c.shape[0]
    pad = np.full((n, length - 1), np.nan)
    h = np.concatenate([pad, np.where(np.isnan(day["h"]), c, day["h"])], axis=1)
    l = np.concatenate([pad, np.where(np.isnan(day["l"]), c, day["l"])], axis=1)
    with np.errstate(all="ignore"):
        hh = np.nanmax(sliding_window_view(h, length, axis=1), axis=-1)
        ll = np.nanmin(sliding_window_view(l, length, axis=1), axis=-1)
        r = np.where(hh > ll, -100.0 * (hh - c) / (hh - ll), np.nan)
    if ewm_span > 1:
        a = 2.0 / (ewm_span + 1.0)
        acc = np.full(n, np.nan)
        for m in range(r.shape[1]):
            x = r[:, m]
            acc = np.where(np.isnan(acc), x, np.where(np.isnan(x), acc, a * x + (1 - a) * acc))
            r[:, m] = acc
    r[:, :max(0, min_bars - 1)] = np.nan
    return np.where(np.isnan(day["c"]), np.nan, r)


def day_change(day, prev):
    """Percent change at each minute's close vs the prior session's last close (causal)."""
    pc = _ffill(prev["c"])[:, -1]
    with np.errstate(all="ignore"):
        return (day["c"] / pc[:, None] - 1.0) * 100.0
