"""Pivot Point SuperTrend (LonesomeTheBlue, TradingView), default settings: pivot period 2, ATR factor 3, ATR period 10.

Port for docs/studies/tight_trail_replay_prereg.json amended_14 (exit variant np_lob_st). Point-in-time: the value at bar i
uses bars <= i only. A pivot high/low at bar i-prd is CONFIRMED at bar i (prd bars later), as Pine's pivothigh(prd, prd).

  ph/pl     high[i-prd] strictly above (low strictly below) every high (low) in the prd bars on each side
  center    first pivot price, then (2 x center + pivot) / 3 at each new pivot
  ATR       Wilder RMA of true range over atr_len bars (seeded with the simple mean of the first atr_len TRs)
  Up / Dn   center -/+ factor x ATR
  TUp       max(Up, TUp[1]) if close[i-1] > TUp[1] else Up         (the green trailing line under an uptrend)
  TDown     min(Dn, TDown[1]) if close[i-1] < TDown[1] else Dn     (the red line over a downtrend)
  trend     +1 if close > TDown[1], -1 if close < TUp[1], else trend[1] (starting +1)
Ties in pivot detection: strict inequality on both sides (conservative; Pine's exact tie rule is not reproduced).
"""
from __future__ import annotations

PRD, FACTOR, ATR_LEN = 2, 3.0, 10


def supertrend(bars, prd: int = PRD, factor: float = FACTOR, atr_len: int = ATR_LEN):
    """bars: [(ts, open, high, low, close), ...] oldest first. -> list of dicts per bar:
    {"trend": +1/-1/None, "tup": float|None, "tdown": float|None, "center": float|None, "atr": float|None}."""
    n = len(bars)
    hi = [float(b[2]) for b in bars]
    lo = [float(b[3]) for b in bars]
    cl = [float(b[4]) for b in bars]
    out = []
    center = None
    atr = None
    trs = []
    tup = tdown = None
    trend = 1
    for i in range(n):
        tr = hi[i] - lo[i] if i == 0 else max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
        if atr is None:
            trs.append(tr)
            if len(trs) == atr_len:
                atr = sum(trs) / atr_len
        else:
            atr = (atr * (atr_len - 1) + tr) / atr_len
        if i >= 2 * prd:
            k = i - prd
            left_h, right_h = hi[k - prd:k], hi[k + 1:i + 1]
            left_l, right_l = lo[k - prd:k], lo[k + 1:i + 1]
            pp = None
            if hi[k] > max(left_h) and hi[k] > max(right_h):
                pp = hi[k]
            elif lo[k] < min(left_l) and lo[k] < min(right_l):
                pp = lo[k]
            if pp is not None:
                center = pp if center is None else (center * 2 + pp) / 3
        if center is None or atr is None:
            out.append({"trend": None, "tup": None, "tdown": None, "center": center, "atr": atr})
            continue
        up, dn = center - factor * atr, center + factor * atr
        prev_tup, prev_tdown = tup, tdown
        tup = max(up, prev_tup) if (prev_tup is not None and cl[i - 1] > prev_tup) else up
        tdown = min(dn, prev_tdown) if (prev_tdown is not None and cl[i - 1] < prev_tdown) else dn
        if prev_tdown is not None and cl[i] > prev_tdown:
            trend = 1
        elif prev_tup is not None and cl[i] < prev_tup:
            trend = -1
        out.append({"trend": trend, "tup": tup, "tdown": tdown, "center": center, "atr": atr})
    return out


def exit_due(bars, now: float, min_bars: int = 30, bar_sec: float = 60.0):
    """amended_14 exit: True when the latest COMPLETED bar (start + bar_sec <= now) has trend -1.
    -> (due: bool | None, info). None = not enough completed bars (caller falls back to its other exits)."""
    done = [b for b in bars if float(b[0]) + bar_sec <= float(now)]
    if len(done) < min_bars:
        return None, {"bars": len(done)}
    st = supertrend(done)
    last = st[-1]
    if last["trend"] is None:
        return None, {"bars": len(done)}
    return last["trend"] == -1, {"bars": len(done), "tup": last["tup"], "close": float(done[-1][4]), "bar_ts": float(done[-1][0])}
