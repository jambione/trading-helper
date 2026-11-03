# Proposal: Morning Catalyst First-Pullback Day Trading Study (`catalyst_pullback`)

**Date:** 2026-10-31  
**Author:** Antigravity (Pair Programming with Operator)  
**Status:** PROPOSAL — Amended after Skeptic Round 1 (All 7 FIX FIRST items applied)  
**Preregistration Target:** `docs/studies/catalyst_pullback_prereg.json`  

---

### 1. Executive Summary & Core Hypothesis

**The Problem:** All technical indicators tested on random or technical-screener momentum stocks fail the cost barrier. A typical technical setup produces an expected gross move of only $+1\text{ to }+3\text{ bp}$, which is completely erased by the $10\text{ to }15\text{ bp}$ round-trip bid-ask spread.

**The Hypothesis:** When a company announces a major fundamental catalyst before the open (confirmed EPS beat, raised full-year guidance, or FDA approval), institutional capital (mutual funds, pensions) is mandated to rebalance large positions. Institutional execution algorithms (VWAP, TWAP) work orders across the morning session ($09:40\text{--}11:30\text{ ET}$). 
* The gross momentum drive on catalyst stocks is **$+50\text{ to }+200\text{ bp}$**, providing ample margin to easily overpower a real consolidated SIP spread.
* Buying the **first 5-minute pullback consolidation** near VWAP after the opening rush ($09:40\text{--}10:30\text{ ET}$) provides a high-conviction, low-risk entry into the institutional trend with an asymmetric risk-to-reward ratio.

---

### 2. Universe & Sample Window

* **Sample Window:** 2024-01-02 through 2026-10-30 (712 trading sessions).
* **Split Halves (Chronological, matching desk standard):**
  * **Half A:** 2024-01-02 .. 2025-05-31 (approx. 355 sessions).
  * **Half B:** 2025-06-01 .. 2026-10-30 (approx. 357 sessions).
* **Two-Stage Fetch Architecture (Respects Alpaca rate limits and disk caps):**
  * **Stage 1 (Universe Screening via Daily & 1m Bars):**
    * Common stocks listed on NYSE or Nasdaq.
    * Triple Price Floor (Skeptic Fix 4): $\min(P_{\text{D-1 close}}, P_{\text{09:30 open}}, P_{\text{entry}}) \ge \$20.00$.
    * 20-day ADV (shifted by 1 day) $\ge \$20\text{M}$.
    * Premarket Open Gap $\ge +3.0\%$ (Official cash open at 09:30 vs. unadjusted $D-1$ close $\ge 1.03$).
    * Opening 10-Minute Relative Volume (Skeptic Fix 1): Total volume from $09:30:00$ to $09:39:59\text{ ET} \ge 2.5\times$ the trailing 20-day 10-minute open volume.
    * *Yield:* Filters ~1,500 symbols down to an average of $2\text{--}5$ candidate symbols per session.
  * **Stage 2 (Catalyst Verification & Microstructure Data):**
    * For candidate symbols only, query historical Benzinga news and fetch 1m SIP bars + NBBO quotes around entry/exit.

---

### 3. Catalyst Classification Rules (Skeptic Fix 3)

News items from Alpaca/Benzinga News must be timestamped between $16:00\text{ ET}$ on session $D-1$ and $09:25\text{ ET}$ on session $D0$ (premarket cutoff, zero lookahead to cash open). The headline must match at least one of three frozen regex categories:
1. **Earnings Beat:**
   * Headline matches: `(?i)\b(beats?|tops?|surpasses?|exceeds?)\b.*\b(eps|earnings|estimates?|views?|rev(enue)?)\b`
   * AND does not match: `(?i)\b(misses?|lags?|falls short|cuts?|lowers?)\b`
2. **Guidance Raised:**
   * Headline matches: `(?i)\b(raises?|boosts?|lifts?|hikes?|upgrades?)\b.*\b(guidance|outlook|forecast|views?|fy\d\d?)\b`
3. **FDA Approval / Clearance:**
   * Headline matches: `(?i)\b(fda\b.*\b(approv|clears?|authoriz)|receives? fda approval)\b`

---

### 4. Setup & Entry Trigger (Skeptic Fixes 1 & 5)

* **Opening Settlement (09:30–09:40 ET):**
  * The first 10 minutes establish the Opening Range High ($H_{10}$) and Low ($L_{10}$), and confirm the RVOL gate with zero lookahead.
  * **Zero entries permitted before 09:40:00 ET.**
* **Pullback Phase (09:40–10:30 ET):**
  * After 09:40, the stock must undergo a consolidation or pullback of at least 1 completed 1-minute down candle where price pulls back toward intraday cash VWAP (calculated from 09:30:00 ET cash open, pullback distance defined as `low <= cash_vwap * 1.01`).
  * Price must remain strictly above the premarket low and above prior day close.
  * Maximum trades per symbol per session: strictly 1 (`max_trades_per_symbol_day = 1`).
* **Resumption Trigger (Bar Close Definition, Skeptic Fix 5):**
  * The first 1-minute candle that closes above the high of the prior pullback candle while price is $\ge \text{cash VWAP}$.
  * **Order Execution:** Entry fills at the SIP Ask price at the open of the immediately following 1-minute candle (charging the full quoted spread).

---

### 5. Exit Rules & Cost Model

* **Initial Stop:** Under the low of the pullback consolidation (capped at max $1.5\%$).
* **Exit Horizons & Comparison (Skeptic Fix 6):**
  * **Fixed Markouts:** 3-minute, 5-minute, 15-minute, and 30-minute markouts.
  * **Live Desk Leash / Trail:** Replay using `leash_replay.walk` / `trail_trend_hold`.
  * **Hard Time Stop:** All open positions closed flat at $11:30\text{ ET}$ (end of morning institutional window; zero afternoon or overnight holding).
* **Cost Charged:** Full round-trip consolidated SIP bid-ask spread at entry (Ask) and exit (Bid), plus $1\text{ bp}$ modeled broker friction.

---

### 6. Controls (Adversarial Nulls)

1. **Control 1 (Timing Null, Skeptic Fix 6):** A random minute entry on the *same* catalyst stock between 09:40 and 11:30 ET, evaluated on the exact same fixed markout horizons (3m, 5m, 15m, 30m, and flat at 11:30 ET).
2. **Control 2 (Catalyst Null):** The exact same first-pullback entry rule run on *non-catalyst* morning gappers (technical movers with gap $\ge 3\%$ and RVOL $\ge 2.5$, but no news). (Tests whether fundamental institutional rebalancing produces higher gross move and win rate than generic technical momentum).

---

### 7. Success Bar & Decision Rule (Skeptic Fix 2)

* **Minimum Events:** $\ge 300$ eligible trades per half across $\ge 40$ distinct sessions.
* **MDE Bar (Recalibrated with day clustering):** Target $\text{MDE} \le 30.0\text{ bp}$ (matching `earnings_drift_prereg.json` standard).
* **Pass Criteria (All must hold):**
  1. **Pooled Net P&L:** Net expectancy $\ge +10.0\text{ bp}$ per trade after full SIP spread and fees.
  2. **Both Halves Positive:** Net expectancy $> 0$ in both Half A and Half B with $t \ge 2.0$.
  3. **Beats Timing Control:** Pullback entry outperforms random-minute control by $\ge +10.0\text{ bp}$ with $t \ge 2.0$.
  4. **Beats Technical Control:** Catalyst gappers outperform non-catalyst gappers by $\ge +10.0\text{ bp}$ with $t \ge 2.0$.
* **Verdict Labels:** `PASS`, `FAIL`, or `UNDERPOWERED`.
