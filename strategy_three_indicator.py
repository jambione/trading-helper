"""
strategy_three_indicator.py — The 3-indicator discretionary strategy, encoded.

Mirrors the manual TradingView workflow:

  BUY  when a bullish MACD cross with WIDE line separation coincides with
       CM RSI-2 < buy_max and rising, and %R Trend Exhaustion rising toward 0.

  SELL when momentum reverses — any (or all) of:
       • bearish MACD cross
       • CM RSI-2 > sell_min and falling
       • %R Trend Exhaustion rolling over from overbought (was above sell_from)

These are pure functions over a bar DataFrame so the SAME logic can drive both
the backtest (backtest_3ind.py) and, later, the live signal engine — no parallel
reimplementation that silently drifts.

No lookahead: every signal at bar i is computed only from data up to and
including i. The backtester fills at bar i+1's open, so a decision made on the
close of bar i never peeks at the bar it trades on.

"Large separation" is defined relative to each ticker's own recent MACD
behaviour: |histogram| >= macd_sep_mult × rolling std(histogram). That matches
the visual intuition ("the gap looks big *for this stock*") and is scale-free
across a $3 micro-cap and a $300 large-cap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signals import (
    compute_cm_rsi_lower,
    compute_macd,
    compute_percent_r_exhaustion,
)

# ── Tunable parameters (all sweepable from the backtest) ──────────────────────
DEFAULT_PARAMS: dict = {
    # CM RSI-2
    "cm_rsi_length":   2,
    "cm_rsi_buy_max":  40.0,    # buy only while CM RSI-2 below this …
    "cm_rsi_sell_min": 90.0,    # … exit when CM RSI-2 above this and falling
    # %R Trend Exhaustion
    "rte_threshold":   20,
    "rte_sell_from":  -10.0,    # %R must have been above this (near 0) to call a top
    # MACD
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "macd_sep_mult":   1.0,     # "large separation" = |hist| ≥ mult × rolling std(hist)
    "macd_sep_window": 50,
    # Alignment / trend
    "confirm_window":  8,       # bars within which the cross + filters must align
    "trend_lookback":  2,       # bars used to judge "rising" / "falling"
    # Exit
    "exit_mode": "any",         # "any" = exit on first reversal signal; "all" = require all 3
}

# Columns the signal functions read, by their numpy-array key.
_ARRAY_COLS = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "cm_rsi": "cm_rsi",
    "s_percentR": "s_percentR", "l_percentR": "l_percentR",
    "macd_line": "macd_line", "macd_signal": "macd_signal_line",
    "macd_hist": "macd_hist", "macd_hist_std": "macd_hist_std",
}


def params(**overrides) -> dict:
    """Return a full parameter dict with the given overrides applied."""
    return {**DEFAULT_PARAMS, **overrides}


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, p: dict | None = None) -> pd.DataFrame:
    """
    Add every indicator the strategy needs to a copy of `df`.
    Reuses the shared signals.py implementations (single source of truth).
    """
    p = {**DEFAULT_PARAMS, **(p or {})}
    df = compute_cm_rsi_lower(df, p)            # → cm_rsi
    df = compute_percent_r_exhaustion(df, p)    # → s_percentR, l_percentR
    df = compute_macd(df, p)                    # → macd_line, macd_signal_line, macd_hist, macd_bull/bear

    # "Large separation" yardstick — this stock's own recent histogram spread.
    w = int(p["macd_sep_window"])
    df["macd_hist_std"] = df["macd_hist"].rolling(w, min_periods=max(5, w // 5)).std()
    return df


def to_arrays(df: pd.DataFrame) -> dict:
    """Extract the columns the signal functions need as numpy arrays (fast access)."""
    out = {key: df[col].to_numpy(dtype=float) for key, col in _ARRAY_COLS.items()}
    out["macd_bull"] = df["macd_bull"].to_numpy(dtype=bool)
    out["macd_bear"] = df["macd_bear"].to_numpy(dtype=bool)
    return out


# ── Small helpers ─────────────────────────────────────────────────────────────

def _rising(s: np.ndarray, i: int, lookback: int) -> bool:
    j = i - lookback
    if j < 0:
        return False
    a, b = s[j], s[i]
    return bool(np.isfinite(a) and np.isfinite(b) and b > a)


def _falling(s: np.ndarray, i: int, lookback: int) -> bool:
    j = i - lookback
    if j < 0:
        return False
    a, b = s[j], s[i]
    return bool(np.isfinite(a) and np.isfinite(b) and b < a)


def _ready(a: dict, i: int, keys) -> bool:
    return all(np.isfinite(a[k][i]) for k in keys)


# ── Signals ───────────────────────────────────────────────────────────────────

def buy_signal(a: dict, i: int, p: dict) -> bool:
    """
    True when, evaluated at bar i (which fills at i+1's open):
      • a bullish MACD cross occurred within the last confirm_window bars, the
        MACD line is still above its signal at i, and the histogram is wide
        (|hist| ≥ macd_sep_mult × rolling std), AND
      • CM RSI-2 was < cm_rsi_buy_max and rising on some bar in the window, AND
      • %R (fast) was rising toward 0 on some bar in the window.

    The two oscillator conditions are checked across the window — not pinned to
    bar i — because CM RSI-2 is fast and the MACD cross lags: by the time MACD
    crosses up, RSI-2 has usually already climbed out of the <40 zone. Requiring
    same-bar coincidence would essentially never fire. The window is strictly
    backward-looking, so there is no lookahead.
    """
    cw, tl = int(p["confirm_window"]), int(p["trend_lookback"])
    if i < cw + tl:
        return False
    if not _ready(a, i, ("macd_line", "macd_signal", "macd_hist", "macd_hist_std")):
        return False

    lo = max(tl, i - cw + 1)

    # MACD: recent bullish cross, still bullish at i, wide separation at i
    if not a["macd_bull"][lo:i + 1].any():
        return False
    if not (a["macd_line"][i] > a["macd_signal"][i]):
        return False
    sep = a["macd_hist_std"][i]
    if not (sep > 0 and a["macd_hist"][i] >= p["macd_sep_mult"] * sep):
        return False

    # CM RSI-2 low and rising — somewhere in the window
    cm_ok = any(a["cm_rsi"][j] < p["cm_rsi_buy_max"] and _rising(a["cm_rsi"], j, tl)
                for j in range(lo, i + 1))
    if not cm_ok:
        return False

    # %R Trend Exhaustion rising toward 0 — somewhere in the window
    rte_ok = any(_rising(a["s_percentR"], j, tl) for j in range(lo, i + 1))
    return rte_ok


def sell_signal(a: dict, i: int, p: dict) -> bool:
    """
    Reversal exit. Three signals, combined per exit_mode:
      • MACD bearish cross within confirm_window
      • CM RSI-2 > cm_rsi_sell_min and falling (somewhere in the window)
      • %R rolling over: was above rte_sell_from (near 0) in the window and falling
    exit_mode="any" (default, safer) exits on the first; "all" requires all three.
    """
    cw, tl = int(p["confirm_window"]), int(p["trend_lookback"])
    if i < cw + tl:
        return False
    lo = max(tl, i - cw + 1)

    macd_down = bool(a["macd_bear"][lo:i + 1].any())

    cm = any(np.isfinite(a["cm_rsi"][j])
             and a["cm_rsi"][j] > p["cm_rsi_sell_min"]
             and _falling(a["cm_rsi"], j, tl)
             for j in range(lo, i + 1))

    window = a["s_percentR"][lo:i + 1]
    window_max = np.nanmax(window) if np.isfinite(window).any() else np.nan
    rte = (np.isfinite(window_max)
           and window_max > p["rte_sell_from"]
           and any(_falling(a["s_percentR"], j, tl) for j in range(lo, i + 1)))

    sigs = (macd_down, cm, rte)
    return all(sigs) if p["exit_mode"] == "all" else any(sigs)


# ── Display breakdown (for the dashboard) ─────────────────────────────────────

def evaluate_state(a: dict, i: int, p: dict) -> dict:
    """
    Return a JSON-serialisable breakdown of the strategy at bar i — the three
    BUY conditions (met/not), the live indicator values, and the authoritative
    buy/sell booleans. Used to render the dashboard's strategy bar.

    Reads only the pre-computed arrays in `a` (no indicator recomputation), so it
    is cheap enough to call on every evaluation.
    """
    cw, tl = int(p["confirm_window"]), int(p["trend_lookback"])
    out = {
        "cm_rsi": None, "cm_rsi_rising": False, "cm_ok": False,
        "pctr": None, "pctr_rising": False, "pctr_ok": False,
        # Slow %R line + deep-oversold band (desk FOCUS uses both lines)
        "pctr_slow": None, "pctr_falling": False, "pctr_slow_falling": False,
        "pctr_deep_os": False,
        "macd_cross": False, "macd_sep_ratio": None, "macd_ok": False,
        "buy": False, "sell": False, "buy_pct": 0,
    }
    if i < cw + tl:
        return out
    lo = max(tl, i - cw + 1)

    # CM RSI-2: < buy_max and rising, somewhere in the window
    cm = a["cm_rsi"][i]
    if np.isfinite(cm):
        out["cm_rsi"] = round(float(cm), 1)
    out["cm_rsi_rising"] = _rising(a["cm_rsi"], i, tl)
    out["cm_ok"] = bool(any(a["cm_rsi"][j] < p["cm_rsi_buy_max"]
                            and _rising(a["cm_rsi"], j, tl)
                            for j in range(lo, i + 1)))

    # %R Trend Exhaustion: rising toward 0, somewhere in the window
    pr = a["s_percentR"][i]
    if np.isfinite(pr):
        out["pctr"] = round(float(pr), 1)
    out["pctr_rising"] = _rising(a["s_percentR"], i, tl)
    out["pctr_ok"] = bool(any(_rising(a["s_percentR"], j, tl) for j in range(lo, i + 1)))

    # Fast + slow %R: deep OS band [-100, -75] and falling toward -100
    # (desk FOCUS long cue — not the same as pctr_ok which is "rising toward 0")
    pr_s = a["l_percentR"][i]
    if np.isfinite(pr_s):
        out["pctr_slow"] = round(float(pr_s), 1)
    out["pctr_falling"] = _falling(a["s_percentR"], i, tl)
    out["pctr_slow_falling"] = _falling(a["l_percentR"], i, tl)
    if (np.isfinite(pr) and np.isfinite(pr_s)
            and -100.0 <= float(pr) <= -75.0
            and -100.0 <= float(pr_s) <= -75.0
            and out["pctr_falling"] and out["pctr_slow_falling"]):
        out["pctr_deep_os"] = True

    # MACD: recent bullish cross, still bullish, wide separation
    line, sig = a["macd_line"][i], a["macd_signal"][i]
    cross = bool(a["macd_bull"][lo:i + 1].any()) and (np.isfinite(line) and np.isfinite(sig) and line > sig)
    out["macd_cross"] = cross
    sep = a["macd_hist_std"][i]
    if np.isfinite(sep) and sep > 0 and np.isfinite(a["macd_hist"][i]):
        out["macd_sep_ratio"] = round(float(a["macd_hist"][i] / sep), 2)
        out["macd_ok"] = bool(cross and a["macd_hist"][i] >= p["macd_sep_mult"] * sep)

    out["buy"] = buy_signal(a, i, p)
    out["sell"] = sell_signal(a, i, p)
    out["buy_pct"] = round((int(out["cm_ok"]) + int(out["pctr_ok"]) + int(out["macd_ok"])) / 3 * 100)
    return out
