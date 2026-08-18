"""
signals.py — Multi-Indicator Strategy with CM RSI-2, OBV Oscillator, and %R Trend Exhaustion
"""

import numpy as np
import pandas as pd


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average"""
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Standard RSI calculation"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def williams_pr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Williams %R calculation"""
    hh = high.rolling(length).max()
    ll = low.rolling(length).min()
    return -100 * (hh - close) / (hh - ll).replace(0, np.nan)


def vwap_calc(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price"""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    if hasattr(df.index, "date"):
        day = pd.Series(df.index.date, index=df.index)
    else:
        day = pd.Series([d.date() for d in df.index], index=df.index)
    cum_tp = (typical * df["volume"]).groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_tp / cum_vol.replace(0, np.nan)


def calc_rvol(df: pd.DataFrame, avg_daily_vol: int = 0) -> pd.Series:
    """Relative Volume calculation"""
    if len(df) < 20:
        return pd.Series(1.0, index=df.index)
    avg_vol = df["volume"].rolling(20).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range"""
    tr1 = df["high"] - df["low"]
    tr2 = abs(df["high"] - df["close"].shift(1))
    tr3 = abs(df["low"] - df["close"].shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index"""
    df = df.copy()
    df["up"] = df["high"].diff()
    df["down"] = -df["low"].diff()
    
    df["plus_dm"] = np.where((df["up"] > df["down"]) & (df["up"] > 0), df["up"], 0)
    df["minus_dm"] = np.where((df["down"] > df["up"]) & (df["down"] > 0), df["down"], 0)
    
    tr = atr(df, period)
    df["plus_di"] = 100 * (df["plus_dm"].ewm(alpha=1/period, adjust=False).mean() / tr)
    df["minus_di"] = 100 * (df["minus_dm"].ewm(alpha=1/period, adjust=False).mean() / tr)
    
    df["dx"] = 100 * (abs(df["plus_di"] - df["minus_di"]) / (df["plus_di"] + df["minus_di"]))
    return df["dx"].ewm(alpha=1/period, adjust=False).mean()


# ============================================================================
# CM RSI 2 (Larry Connors RSI-2 Strategy - Lower)
# Based on Pine Script by ChrisMoody
# ============================================================================

def compute_cm_rsi_lower(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    CM RSI-2 Logic:
    - RSI with configurable-period RMA smoothing (default 2, per Larry Connors)
    - Green (bullish) when: close > SMA(200) AND close < SMA(5) AND RSI < 10
    - Approaching signal when RSI < cm_rsi_oversold threshold
    """
    df = df.copy()

    period = max(2, int(cfg.get("cm_rsi_length", 2)))
    alpha  = 1.0 / period
    approach_lvl = float(cfg.get("cm_rsi_oversold", 25))

    delta = df["close"].diff()
    up   = delta.clip(lower=0).ewm(alpha=alpha, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=alpha, adjust=False).mean()
    rsi2 = np.where(down == 0, 100, np.where(up == 0, 0, 100 - (100 / (1 + up / down))))
    df["cm_rsi"] = pd.Series(rsi2, index=df.index)

    df["ma_200"] = sma(df["close"], 200)
    df["ma_5"]   = sma(df["close"], 5)

    df["cm_rsi_approaching"] = (
        (df["close"] > df["ma_200"]) &
        (df["close"] < df["ma_5"]) &
        (df["cm_rsi"] < approach_lvl)
    )
    df["cm_rsi_bullish"] = (
        (df["close"] > df["ma_200"]) &
        (df["close"] < df["ma_5"]) &
        (df["cm_rsi"] < 10)
    )
    return df


# ============================================================================
# ON BALANCE VOLUME OSCILLATOR
# Based on Pine Script by LazyBear
# ============================================================================

def compute_obv_oscillator(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    OBV Oscillator Logic:
    - OBV = Cumulative volume (positive when close up, negative when close down)
    - Oscillator = OBV - EMA(OBV, length)
    - Bullish when: oscillator > 0 AND oscillator rising (trending up)
    """
    df = df.copy()
    
    length = cfg.get("obv_length", 20)
    
    # Calculate OBV — use np.where to avoid pandas SettingWithCopyWarning
    change     = df["close"].diff()
    signed_vol = np.where(change > 0, df["volume"],
                 np.where(change < 0, -df["volume"], 0.0))
    df["obv"]  = pd.Series(signed_vol, index=df.index).cumsum()
    
    # Calculate EMA of OBV
    df["obv_ema"] = ema(df["obv"], length)
    
    # Calculate oscillator
    df["obv_osc"] = df["obv"] - df["obv_ema"]
    
    # Check if oscillator is above zero and trending up
    df["obv_above_zero"] = df["obv_osc"] > 0
    df["obv_rising"] = df["obv_osc"] > df["obv_osc"].shift(1)
    df["obv_trending_up"] = df["obv_above_zero"] & df["obv_rising"]
    
    return df


# ============================================================================
# %R TREND EXHAUSTION
# Based on Pine Script by upslidedown
# ============================================================================

def _resampled_percent_r(df: pd.DataFrame, timeframe: str,
                         length: int) -> pd.Series | None:
    """%R computed on coarser bars, mapped back onto the native index.

    Returns None when the frame cannot support it — no time index, or fewer
    than `length` resampled bars — so the caller can fall back rather than
    publish a column of NaN.

    Forward-filled and SHIFTED by one coarse bar. A 15-minute bar is not
    complete until its close, so using its value inside itself would let a
    signal depend on prices later than the moment being evaluated. Shifting
    means bars during 10:00-10:15 read the bar that closed at 10:00. This is
    the same lookahead trap the buy_signal docstring guards against on the
    native series, and it is easier to introduce here.
    """
    if df.empty:
        return None
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        # The signal engine's fetch_bars returns a RangeIndex with the
        # timestamp in a `time` column, so requiring a DatetimeIndex here made
        # this fall back to the native long lookback on every live symbol —
        # the resample would have run in tests and never once in production.
        if "time" not in df.columns:
            return None
        try:
            idx = pd.to_datetime(df["time"], utc=True, format="mixed")
        except Exception:
            try:
                idx = pd.to_datetime(df["time"], utc=True)
            except Exception:
                return None
        if idx.isna().any():
            return None
    try:
        src = df[["high", "low", "close"]].set_axis(idx)
        agg = src.resample(timeframe, label="right", closed="right").agg(
            {"high": "max", "low": "min", "close": "last"}).dropna()
    except Exception:
        return None
    if len(agg) < max(2, int(length)):
        return None
    pr = williams_pr(agg["high"], agg["low"], agg["close"], int(length))
    pr = pr.ewm(span=3, adjust=False).mean().shift(1)
    out = pr.reindex(idx, method="ffill")
    # Hand back a series aligned to the CALLER's index, not the datetime one we
    # built to resample with.
    out = pd.Series(out.to_numpy(), index=df.index)
    # All-NaN means the resample produced nothing usable at this index.
    return None if not out.notna().any() else out


def compute_percent_r_exhaustion(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    %R Trend Exhaustion Logic (Updated for Multiple Oversold Support):
    - Uses dual %R: Fast (21) and Slow (112)
    - Overbought zone: %R >= -threshold
    - Oversold zone: %R <= -100 + threshold
    
    REWORKED: Now supports tracking "Multiple Oversold" indicators.
    """
    df = df.copy()

    threshold = cfg.get("rte_threshold", 20)

    # Fast line: native bars. This is the short scale.
    fast_span = max(1, int(cfg.get("rte_fast_ewm_span", 7) or 7))
    s_pr = williams_pr(df["high"], df["low"], df["close"],
                       int(cfg.get("rte_fast_length", 21)))
    s_percentR = s_pr.ewm(span=fast_span, adjust=False).mean()

    # Slow line matches TradingView %R Trend Exhaustion [upslidedown]:
    # %R(112) EMA(3) on the SAME bars as the fast line. A coarser resample
    # (rte_slow_timeframe, e.g. "15min") is opt-in for research only.
    slow_tf = cfg.get("rte_slow_timeframe", None)
    if isinstance(slow_tf, str) and slow_tf.strip().lower() in (
            "", "none", "native", "0", "false"):
        slow_tf = None
    slow_len = int(cfg.get("rte_slow_length", 21))
    slow_span = max(1, int(cfg.get("rte_slow_ewm_span", 3) or 3))
    l_percentR = None
    if slow_tf:
        l_percentR = _resampled_percent_r(df, slow_tf, slow_len)
    if l_percentR is None:
        l_pr = williams_pr(df["high"], df["low"], df["close"],
                           int(cfg.get("rte_slow_native_length", 112)))
        l_percentR = l_pr.ewm(span=slow_span, adjust=False).mean()

    df["s_percentR"] = s_percentR
    df["l_percentR"] = l_percentR
    
    # Overbought/Oversold conditions
    df["overbought"] = (s_percentR >= -threshold) & (l_percentR >= -threshold)
    df["oversold"]   = (s_percentR <= -100 + threshold) & (l_percentR <= -100 + threshold)
    df["rte_gap"] = (s_percentR - l_percentR).abs()
    try:
        confluence = float(cfg.get("rte_confluence_max", 15) or 15)
    except (TypeError, ValueError):
        confluence = 15.0
    df["rte_tight"] = df["overbought"] & (df["rte_gap"] <= confluence)
    df["rte_strong"] = df["rte_tight"]
    
    # Track consecutive bars in zone
    ob_groups = (df["overbought"] != df["overbought"].shift()).cumsum()
    os_groups = (df["oversold"]   != df["oversold"].shift()).cumsum()
    df["ob_consecutive"] = df.groupby(ob_groups).cumcount().add(1).where(df["overbought"], 0)
    df["os_consecutive"] = df.groupby(os_groups).cumcount().add(1).where(df["oversold"],   0)
    
    # "Multiple Oversold" Indicator tracking
    # We define a signal when multiple oversold levels are hit
    # e.g. s_percentR is deep OS, l_percentR is OS, and we've been here for N bars
    min_bars = cfg.get("rte_min_boxes", 2)
    df["signal_multiple_os"] = (
        (s_percentR <= -100 + threshold) & 
        (l_percentR <= -100 + threshold) & 
        (df["os_consecutive"] >= min_bars)
    )

    # Legacy mappings
    df["ob_trend_start"] = df["overbought"] & df["overbought"].shift(1).fillna(False)
    df["ob_reversal"]    = (~df["overbought"]) & df["overbought"].shift(1).fillna(False)
    df["os_trend_start"] = df["oversold"] & df["oversold"].shift(1).fillna(False)
    df["os_reversal"]    = (~df["oversold"]) & df["oversold"].shift(1).fillna(False)
    
    df["rte_reversal"] = df["ob_reversal"] | df["os_reversal"]
    df["ob_on_deck"] = df["ob_trend_start"] & (df["ob_consecutive"] >= min_bars)
    df["os_on_deck"] = df["os_trend_start"] & (df["os_consecutive"] >= min_bars)
    df["rte_final"] = ((s_percentR + l_percentR) / 2).round(2)
    
    return df


# ============================================================================
# OTHER INDICATORS (kept for compatibility)
# ============================================================================

def compute_rmi(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Relative Momentum Index (placeholder)"""
    df = df.copy()
    df["rmi_signal"] = pd.Series(False, index=df.index)
    return df


def compute_volume_trending_up(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Volume trending up (placeholder - OBV oscillator is more accurate)"""
    df = df.copy()
    df["volume_trending_up"] = pd.Series(False, index=df.index)
    return df


def compute_macd(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """MACD calculation for additional confirmation"""
    df = df.copy()
    fast = cfg.get("macd_fast", 12)
    slow = cfg.get("macd_slow", 26)
    sig = cfg.get("macd_signal", 9)
    
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    
    df["macd_line"] = ema_fast - ema_slow
    df["macd_signal_line"] = ema(df["macd_line"], sig)
    df["macd_hist"] = df["macd_line"] - df["macd_signal_line"]
    
    # Bull/bear crossover
    df["macd_bull"] = (
        (df["macd_line"] > df["macd_signal_line"]) &
        (df["macd_line"].shift(1) <= df["macd_signal_line"].shift(1))
    )
    df["macd_bear"] = (
        (df["macd_line"] < df["macd_signal_line"]) &
        (df["macd_line"].shift(1) >= df["macd_signal_line"].shift(1))
    )
    
    return df


# ============================================================================
# MAIN SIGNAL COMPUTATION
# ============================================================================

def compute_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Main signal computation (Reworked Strategy):
    1. Volume Surge Indicator (Mandatory)
    2. CM RSI 2 (Mandatory)
    3. Multiple Oversold Indicators from Percent R Exhaustion (Mandatory)
    """
    df = df.copy()
    
    # Compute component indicators
    df = compute_cm_rsi_lower(df, cfg)
    df = compute_obv_oscillator(df, cfg)
    df = compute_percent_r_exhaustion(df, cfg)
    
    # Helper indicators
    df["atr"] = atr(df)
    df["rvol"] = calc_rvol(df)
    
    # 1. Volume surge (Configurable threshold)
    vol_mult = float(cfg.get("volume_surge_mult", 1.5))
    df["volume_surge"] = df["volume"] > (df["volume"].rolling(20).mean() * vol_mult)
    
    # 2. CM RSI 2 Bullish (UsesLarry Connors logic)
    df["signal_cm_rsi"] = df["cm_rsi_bullish"]
    
    # 3. Multiple Oversold (Percent R logic)
    df["signal_multiple_os"] = df["signal_multiple_os"]

    # Two-tier logic:
    # SETUP (ON_DECK): %R exhaustion — both fast and slow %R in oversold zone for rte_min_boxes bars
    # ENTRY (BUY):    setup active AND RSI-2 approaching oversold (< cm_rsi_oversold) AND close > SMA(200)
    strategy = cfg.get("strategy", "multiple_os").lower()

    if strategy == "multiple_os":
        buy_condition = df["signal_multiple_os"] & df["cm_rsi_approaching"]
    else:
        # Fallback scoring
        df["score"] = 0
        df.loc[df["signal_multiple_os"], "score"] += 5
        df.loc[df["cm_rsi_approaching"],  "score"] += 5
        buy_condition = (df["score"] >= 8)

    df["signal"] = "HOLD"
    df.loc[buy_condition, "signal"] = "BUY"
    
    df["signal_off_deck_buy"] = False
    
    # Williams %R for additional context
    df["wr"] = williams_pr(df["high"], df["low"], df["close"], cfg.get("wr_length", 14))
    
    # Add aliases for dashboard compatibility
    df["rte_fast"] = df["s_percentR"]  # Fast %R line (smoothed 21-period)
    df["rte_slow"] = df["l_percentR"]  # Slow %R line (smoothed 112-period)
    df["rsi2"] = df["cm_rsi"]         # CM RSI-2 value
    df["rmi"] = df["cm_rsi"]          # Also alias as rmi for config-based threshold check
    df["rmi_signal"] = df["cm_rsi_approaching"]  # RSI-2 pass signal
    
    # Legacy names for backward compatibility with other parts of the system
    df["vol_trend_up"] = df["obv_trending_up"]   # OBV oscillator trending up
    df["volume_trend_up"] = df["obv_trending_up"]
    
    # Add box tracking for streak display
    df["rte_boxes_streak"] = df["os_consecutive"].fillna(0).astype(int)
    df["rte_boxes_completed"] = df["os_consecutive"].fillna(0).astype(int)
    
    # Add RSI column for backward compatibility (some code uses "rsi" instead of "rsi2" or "cm_rsi")
    df["rsi"] = df["cm_rsi"]
    
    # Add EMA columns for dashboard compatibility
    df["ema_short"] = ema(df["close"], cfg.get("ema_short", 8))
    df["ema_long"] = ema(df["close"], cfg.get("ema_long", 21))
    
    return df
