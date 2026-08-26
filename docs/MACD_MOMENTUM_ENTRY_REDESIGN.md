# MACD Momentum Entry & Real-Time Ratchet Stop Redesign

**Date:** 2026-08-26  
**Status:** Implemented & Verified in Test Suite (405 tests passing)

---

## 1. Overview & Objectives

This redesign transitions the desk from %R Trend Exhaustion (`EXH`) and Connors RSI (`CM RSI-2`) entries to a pure **MACD Bullish Momentum Crossover** entry system with line separation gap validation.

Additionally, position management rules have been updated:
1. **Minimum Hold Duration:** Set to **30 seconds** (`ai_exit_min_hold_sec = 30`).
2. **Real-Time Ratchet Stop Floor:** Trailing ratchet stop updates continuously in real time with price ticks. Once the 30-second minimum hold duration elapses, a hard floor of at least **`entry price + $0.01`** is locked in (unless the trailing high-water mark stop is already higher).
3. **State Blocker Clarity:** Real-time indicator validation reasons are formatted and displayed directly in the dashboard **State** column.

---

## 2. MACD Momentum Entry Specification

### Mathematical Model
1. **Fast Line ($MACD_{\text{line}}$):**
   $$\text{EMA}_{12}(\text{Price}) - \text{EMA}_{26}(\text{Price})$$
2. **Slow Signal Line ($MACD_{\text{signal}}$):**
   $$\text{EMA}_9(MACD_{\text{line}})$$
3. **Histogram / Separation Gap ($MACD_{\text{gap}}$):**
   $$\text{Gap} = MACD_{\text{line}} - MACD_{\text{signal}}$$
4. **Separation Ratio ($MACD_{\text{ratio}}$):**
   $$\text{Separation Ratio} = \frac{\text{Gap}}{\sigma_{\text{hist}}(\text{window}=50)}$$

### Buy / Arming Rules (`macd_allows_buy`)
A position is armed to open when all of the following conditions hold simultaneously on the streaming 1-minute chart:
- **Bullish Trend Alignment:** $\text{Fast Line} > \text{Slow Signal Line}$ ($\text{Gap} > 0$).
- **Recent Bullish Crossover:** Fast line crossed above the signal line within the last `confirm_window = 8` bars.
- **Minimum Gap Threshold:** $\text{Gap} \ge 0.005$ (`macd_min_gap`) to avoid flat noise.
- **Wide Line Separation:** $\text{Gap} \ge 0.8 \times \sigma_{\text{hist}}$ (`macd_sep_mult = 0.8`).

---

## 3. Position Management & Ratchet Stop

### Real-Time Trailing & Breakeven Floor
- **0 to 30 Seconds:** The ratchet stop trails dynamically with the live price (`last - give`), allowing the trade to breathe during initial fill volatility without premature exit muzzling.
- **After 30 Seconds:** Once `time.time() - entry_time >= 30`, the ratchet stop floor is enforced at:
  $$\text{Stop} = \max(\text{Current Trailing Stop}, \text{Entry Price} + \$0.01)$$

---

## 4. Dashboard UI & State Blocker Mapping

The dashboard replaces the legacy `EXH` and `RSI` columns with a unified **`MACD Gap`** column:

### Table Columns
`Ticker` | `State` | `Last` | `Entry` | `Stop` | `Hold` | `MACD Gap` | `P&L`

### MACD Gap Styling
- **Wide Bullish Separation ($\ge 0.8\times \sigma$ or $\ge +0.015$):** Highlighted in green with arrow and ratio (e.g. `▲ +0.082 (1.4×)`).
- **Bullish Moderate:** Light green `▲ +0.035`.
- **Flat / Narrow ($|\text{gap}| \le 0.002$):** Amber `0.000`.
- **Bearish ($\text{Fast} < \text{Slow}$):** Red `▼ -0.040`.

### State Column Blocker Labels & Tooltips

| Blocker Code | Label | Hover Tooltip | Description |
| :--- | :--- | :--- | :--- |
| `macd_bearish` | **`MACD bear`** | `fast {f} <= slow {s} (gap {g})` | Fast line is below slow signal line |
| `macd_gap_too_close` | **`MACD narrow`** | `gap {g} < min {min_gap}` | Line separation is under absolute floor (`0.005`) |
| `macd_gap_insufficient` | **`MACD gap low`** | `gap {g} < 0.8x std ({thresh})` | Line separation is under volatility threshold |
| `no_macd_data` | **`no MACD`** | `no realtime MACD (needs 1-min bars)` | Insufficient OHLC bars for MACD computation |
| `macd_no_recent_cross` | **`wait cross`** | `no bullish cross in confirm window` | Awaiting recent bullish crossover |
| `macd_bullish_gap` | **`buy`** | `Bullish cross + wide separation confirmed` | Fully armed and eligible to enter |

---

## 5. Configuration Settings

| Parameter | Default | Location | Description |
| :--- | :--- | :--- | :--- |
| `ai_exit_min_hold_sec` | `30` | `config/bot_config.json`, `config.py` | Minimum hold duration in seconds |
| `ai_watch_arm_require_macd` | `true` | `config/bot_config.json`, `config.py` | Enforces MACD bullish crossover entry gate |
| `ai_watch_tv_exh_rsi` | `false` | `config/bot_config.json`, `config.py` | Disables legacy EXH/RSI conjunction gate |
| `ai_watch_arm_require_cm_rsi` | `false` | `config/bot_config.json`, `config.py` | Bypasses Connors RSI-2 requirement |
| `ai_watch_require_exhaustion_data` | `false` | `config/bot_config.json`, `config.py` | Bypasses %R exhaustion requirement |
| `macd_min_gap` | `0.005` | `config/bot_config.json`, `config.py` | Minimum absolute fast/slow line separation |
| `macd_sep_mult` | `0.8` | `config/bot_config.json`, `config.py` | Multiplier for rolling std histogram separation |
| `macd_sep_window` | `50` | `config/bot_config.json`, `config.py` | Lookback window for histogram standard deviation |
