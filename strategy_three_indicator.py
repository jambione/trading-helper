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
    # CM RSI-2 (Connors 2-period — fast by design, so it is a FILTER here,
    # never a standalone trigger: an entry also needs price inside the pullback
    # zone and %R exhaustion confirming.)
    "cm_rsi_length":   2,
    "cm_rsi_buy_max":  30.0,    # oversold: dipped below this, then turning up
    # 90, not 70. RSI-2 sits above 70 on ~34% of bars, so a 70 level marks an
    # ordinary bounce rather than a stretched tape: measured on 5 sessions of
    # IEX minute bars across DKNG/UBER/SNAP/AMD/SOFI, a 70-rollover came before
    # the +7.5% target in 45 of 45 entries. 90 is Connors' own overbought level
    # (~12% of bars) and is what this started at.
    "cm_rsi_sell_min": 90.0,    # … reached above this, then rolled over
    # %R Trend Exhaustion — TWO TIMEFRAMES, which is the point of it.
    # The long scale says the move is exhausted (the setup); the short scale
    # says it is turning now (the trigger). Both lines used to be computed on
    # the native 1-minute series, ~21m vs ~112m, which is not two timeframes so
    # much as one indicator sampled twice — and on thin IEX names the 112-bar
    # window often had no valid span, so the long line published null.
    # See signals._resampled_percent_r.
    "rte_threshold":   20,
    "rte_fast_length": 21,       # native bars
    "rte_slow_timeframe": None,      # None = TV native %R(112) on the same bars
    "rte_slow_length": 21,       # 21 x 15m ≈ 5.25h vs the fast line's 21m
    "rte_slow_native_length": 112,   # fallback when resampling is impossible
    "rte_slow_threshold": 20,    # exhaustion band for the long line
    # The long line's own lookback, in NATIVE bars — ~4 coarse bars at 15m.
    # It cannot share confirm_window (8 native bars): asking an indicator built
    # from 15-minute bars to be extreme inside 8 minutes is a scale mismatch,
    # and it silenced the strategy completely when first written that way.
    "rte_slow_window": 60,
    "rte_require_slow": True,    # False restores the single fast-line test
    "rte_sell_from":  -10.0,    # %R must have been above this (near 0) to call a top
    # Entry filters: whether CM RSI-2 and %R Exhaustion are required for BUY
    "require_macd":    True,
    "require_cm_rsi":  True,    # False = MACD alone triggers entry (ignoring RSI)
    "require_pctr":    True,    # False = MACD alone triggers entry (ignoring EXH)
    "macd_min_gap":    0.005,   # min absolute line separation (hist)
    # MACD
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "macd_sep_mult":   0.8,     # "large separation" = |histogram| ≥ mult × rolling std(histogram)
    "macd_sep_window": 50,
    # Alignment / trend
    "confirm_window":  8,       # bars within which the cross + filters must align
    "trend_lookback":  2,       # bars used to judge "rising" / "falling"
    # Exit
    "exit_mode": "any",         # "any" = exit on first reversal signal; "all" = require all
    # WHICH reversal signals count. MACD is excluded by default: it is the
    # laggard of the three (see buy_signal), so under exit_mode="any" a lone
    # bearish cross closed positions that CM RSI-2 and %R both still called
    # healthy. CM RSI-2 and %R exhaustion are the operator's actual sell
    # signals. Put "macd" back in this list to restore the old behaviour.
    "exit_signals": ("cm", "rte"),
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

def _cm_ok(a: dict, i: int, p: dict, lo: int, tl: int) -> bool:
    """CM RSI-2 reached oversold in the window AND is turning up *at bar i*.

    "Rising somewhere in the window" was true ~85% of bars — combined with a
    dip that also happens constantly on a 2-period RSI, the pair passed on
    67-79% of all bars and gated nothing. Pinning the turn to the current bar
    is what makes "trended down below 30, then turned up" mean something.
    """
    w = a["cm_rsi"][lo:i + 1]
    if not np.isfinite(w).any():
        return False
    return bool(np.nanmin(w) < p["cm_rsi_buy_max"] and _rising(a["cm_rsi"], i, tl))


def _pctr_ok(a: dict, i: int, p: dict, lo: int, tl: int) -> bool:
    """%R exhaustion on the LONG scale, with the short scale confirming the turn.

    The old test was a bare `any(_rising(...))` with no level at all — an
    indicator called "exhaustion" that never checked exhaustion, true ~90% of
    bars. Oversold is %R <= -100 + rte_threshold, the same band
    compute_percent_r_exhaustion uses.

    It then checked that band on `s_percentR`, the FAST line, so the desk ran
    two short-scale indicators (CM RSI-2 and %R-fast) that largely agree,
    rather than the two timeframes the strategy is built on. The slow line was
    computed, published as pctr_slow, and gated on by nothing.

    Split by role, which is what the operator actually trades:
      • the LONG scale decides whether the move is exhausted (the setup),
      • the SHORT scale decides whether it is turning now (the trigger).

    Requiring exhaustion on both was rejected: the fast line leaves the
    oversold band within a bar or two of turning, so demanding it be there at
    the moment it is also rising is the same near-impossible same-bar ask that
    _cm_ok documents. `rte_require_slow` restores the old single-line test.
    """
    fast = a["s_percentR"]
    if not np.isfinite(fast[lo:i + 1]).any():
        return False
    oversold = -100.0 + float(p["rte_threshold"])

    if not bool(p.get("rte_require_slow", True)):
        w = fast[lo:i + 1]
        return bool(np.nanmin(w) <= oversold and _rising(fast, i, tl))

    slow = a.get("l_percentR")
    if slow is None:
        # Absence is not a pass. The long scale is the setup; without it there
        # is only a fast oscillator twitching, which is what this replaced.
        return False
    # The long scale gets its OWN window. `lo` is the fast line's
    # confirm_window — 8 native bars — and asking an indicator built from
    # 15-minute bars to be extreme inside 8 minutes is a scale mismatch, not a
    # strict gate: on a 600-bar fixture the slow line was in the band on 30
    # bars against the fast line's 244, and the intersection with cm_ok was
    # empty, so the whole strategy stopped firing. Measured in native bars so
    # it stays meaningful whatever the resample period is.
    slow_win = int(p.get("rte_slow_window", 60))
    s_lo = max(0, i - max(1, slow_win) + 1)
    w_slow = slow[s_lo:i + 1]
    if not np.isfinite(w_slow).any():
        return False
    slow_band = -100.0 + float(p.get("rte_slow_threshold", p["rte_threshold"]))
    if not (np.nanmin(w_slow) <= slow_band):
        return False
    # Trigger: the short scale turning up. Level on the long line, timing on
    # the short one.
    return bool(_rising(fast, i, tl))


def buy_signal(a: dict, i: int, p: dict) -> bool:
    """
    True when, evaluated at bar i (which fills at i+1's open):
      • a bullish MACD cross occurred within the last confirm_window bars, the
        MACD line is still above its signal at i, and the histogram is wide
        (|hist| ≥ macd_sep_mult × rolling std), AND
      • CM RSI-2 dipped below cm_rsi_buy_max inside the window and is rising
        AT BAR i (see _cm_ok), AND
      • %R (fast) reached the oversold band inside the window and is rising
        AT BAR i (see _pctr_ok).

    The dip and the turn are sequential, not same-bar: a 2-period RSI is still
    falling while it is under the level and clears it within a bar or two once
    it turns, so requiring both on one bar never fires. But the TURN must be
    current — accepting "rose somewhere in the window" made the pair true on
    67-79% of all bars, which gates nothing. The window is strictly
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
    min_gap = float(p.get("macd_min_gap", 0.005) or 0.005)
    if not (a["macd_hist"][i] >= min_gap):
        return False
    if sep > 0 and not (a["macd_hist"][i] >= p["macd_sep_mult"] * sep):
        return False

    # Optional CM RSI-2 filter (bypassed when require_cm_rsi is False)
    if bool(p.get("require_cm_rsi", False)):
        cm_ok = _cm_ok(a, i, p, lo, tl)
        if not cm_ok:
            return False

    # Optional %R Exhaustion filter (bypassed when require_pctr is False)
    if bool(p.get("require_pctr", False)):
        return _pctr_ok(a, i, p, lo, tl)

    return True


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

    # RSI must have REACHED overbought inside the window and then rolled over —
    # not merely be below the level. Straight after a buy at RSI<30 the reading
    # is already under 70, so a bare "below 70" test would exit on the next bar
    # and the trade could never develop. Same shape as the %R exhaustion exit
    # below ("must have been above rte_sell_from to call a top").
    cm_window = a["cm_rsi"][lo:i + 1]
    cm_max = np.nanmax(cm_window) if np.isfinite(cm_window).any() else np.nan
    cm = (np.isfinite(cm_max)
          and cm_max > p["cm_rsi_sell_min"]
          and any(_falling(a["cm_rsi"], j, tl) for j in range(lo, i + 1)))

    window = a["s_percentR"][lo:i + 1]
    window_max = np.nanmax(window) if np.isfinite(window).any() else np.nan
    rte = (np.isfinite(window_max)
           and window_max > p["rte_sell_from"]
           and any(_falling(a["s_percentR"], j, tl) for j in range(lo, i + 1)))

    by_name = {"macd": macd_down, "cm": cm, "rte": rte}
    wanted = p.get("exit_signals") or ("cm", "rte")
    if isinstance(wanted, str):                      # env override: "cm,rte"
        wanted = [s.strip() for s in wanted.split(",") if s.strip()]
    sigs = [by_name[k] for k in wanted if k in by_name]
    if not sigs:
        return False
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
        "pctr_slow_rising": False,
        "pctr_deep_os": False,
        "pctr_ob": False, "pctr_tight": False, "pctr_gap": None,
        "cm_rsi_low": False, "cm_rsi_green": False,
        "macd_cross": False, "macd_sep_ratio": None, "macd_ok": False,
        # Unknown, not "flat": too few bars cannot say a gap is holding.
        "macd_gap_rising": None, "macd_gap_falling": None,
        "macd_gap_prev": None,
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
    # Must stay identical to buy_signal's cm_ok — this is the published flag
    # (signal_state.json -> /api/state) that the AI watch arms on, so any drift
    # here means the desk buys on a different rule than the strategy tests.
    out["cm_ok"] = _cm_ok(a, i, p, lo, tl)

    # %R Trend Exhaustion: rising toward 0, somewhere in the window
    pr = a["s_percentR"][i]
    if np.isfinite(pr):
        out["pctr"] = round(float(pr), 1)
    out["pctr_rising"] = _rising(a["s_percentR"], i, tl)
    out["pctr_ok"] = _pctr_ok(a, i, p, lo, tl)

    # Fast + slow %R: deep OS band [-100, -75] and falling toward -100
    # (desk FOCUS long cue — not the same as pctr_ok which is "rising toward 0")
    pr_s = a["l_percentR"][i]
    if np.isfinite(pr_s):
        out["pctr_slow"] = round(float(pr_s), 1)
    out["pctr_falling"] = _falling(a["s_percentR"], i, tl)
    out["pctr_slow_falling"] = _falling(a["l_percentR"], i, tl)
    out["pctr_slow_rising"] = _rising(a["l_percentR"], i, tl)
    if (np.isfinite(pr) and np.isfinite(pr_s)
            and -100.0 <= float(pr) <= -75.0
            and -100.0 <= float(pr_s) <= -75.0
            and out["pctr_falling"] and out["pctr_slow_falling"]):
        out["pctr_deep_os"] = True

    # Desk / watchlist: both-line overbought (red boxes) and tightness.
    try:
        thr = float(p.get("rte_threshold", 20) or 20)
    except (TypeError, ValueError):
        thr = 20.0
    try:
        tight_max = float(p.get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        tight_max = 15.0
    if np.isfinite(pr) and np.isfinite(pr_s):
        out["pctr_ob"] = bool(float(pr) >= -thr and float(pr_s) >= -thr)
        out["pctr_gap"] = round(abs(float(pr) - float(pr_s)), 2)
        out["pctr_tight"] = bool(out["pctr_ob"] and out["pctr_gap"] <= tight_max)
    else:
        out["pctr_ob"] = False
        out["pctr_gap"] = None
        out["pctr_tight"] = False
    try:
        buy_max = float(p.get("cm_rsi_buy_max", 10) or 10)
    except (TypeError, ValueError):
        buy_max = 10.0
    out["cm_rsi_low"] = bool(np.isfinite(cm) and float(cm) <= buy_max)
    out["cm_rsi_green"] = bool(out.get("cm_ok") and np.isfinite(cm) and float(cm) < 10.0)

    # MACD: recent bullish cross, still bullish, wide separation
    line, sig = a["macd_line"][i], a["macd_signal"][i]
    cross = bool(a["macd_bull"][lo:i + 1].any()) and (np.isfinite(line) and np.isfinite(sig) and line > sig)
    out["macd_fast"] = round(float(line), 4) if np.isfinite(line) else None
    out["macd_slow"] = round(float(sig), 4) if np.isfinite(sig) else None
    out["macd_gap"] = round(float(a["macd_hist"][i]), 4) if np.isfinite(a["macd_hist"][i]) else None
    out["macd_hist"] = out["macd_gap"]
    out["macd_bull"] = bool(np.isfinite(line) and np.isfinite(sig) and line > sig)
    out["macd_cross"] = cross
    sep = a["macd_hist_std"][i]
    if np.isfinite(sep) and sep > 0 and np.isfinite(a["macd_hist"][i]):
        out["macd_sep_ratio"] = round(float(a["macd_hist"][i] / sep), 2)
        out["macd_ok"] = bool(cross and a["macd_hist"][i] >= p["macd_sep_mult"] * sep)
    # Is the gap OPENING or CLOSING? The level says the lines are apart; it
    # cannot say whether they are still separating. A +0.03 gap that was
    # +0.08 two bars ago is momentum dying, and entering it buys the fade —
    # the same distinction cm_rsi_rising draws for RSI, on the same
    # trend_lookback. The previous value rides along so a slice can sweep
    # the rule instead of trusting the boolean.
    out["macd_gap_rising"] = _rising(a["macd_hist"], i, tl)
    out["macd_gap_falling"] = _falling(a["macd_hist"], i, tl)
    prev_gap = a["macd_hist"][i - tl] if i - tl >= 0 else np.nan
    out["macd_gap_prev"] = (
        round(float(prev_gap), 4) if np.isfinite(prev_gap) else None)

    out["buy"] = buy_signal(a, i, p)
    out["sell"] = sell_signal(a, i, p)
    out["buy_pct"] = round((int(out["cm_ok"]) + int(out["pctr_ok"]) + int(out["macd_ok"])) / 3 * 100)
    return out
