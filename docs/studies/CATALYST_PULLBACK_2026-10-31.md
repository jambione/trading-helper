# Study Report: Morning Catalyst First-Pullback (`catalyst_pullback`)

**Date:** 2026-10-31  
**Status:** CLOSED — Verdict: `FAIL` (High Confidence, 2024–2026)  
**Pre-Registration:** [`docs/studies/catalyst_pullback_prereg.json`](catalyst_pullback_prereg.json)  
**Artifacts:** `ai_reports/catalyst_pullback/` (`candidates.json`, `classified_candidates.json`, `count.json`, `result.json`)  

---

### Executive Summary

We evaluated whether buying the "first pullback consolidation" near cash VWAP ($09:40\text{--}10:30\text{ ET}$) on pre-market earnings and news gappers overcomes the bid-ask spread and generates positive net day-trading expectancy.

* **Sample & Universe:** 684 trading sessions ($2024-01-02\text{ to }2026-10-30$). Screened 3,317 morning gappers ($P \ge \$20$, $\text{ADV20} \ge \$20\text{M}$, Gap $\ge +3.0\%$) and parsed historical Benzinga headlines ($16:00\text{ D-1 to }09:25\text{ D0}$) into 1,112 verified fundamental catalyst events (Earnings Beats, Guidance Raises, FDA Approvals) and 2,205 technical gapper controls.
* **Power:** Both halves fully powered. Target MDE was $\le 30.0\text{ bp}$. Realized MDE: Half A **13.81 bp**, Half B **15.20 bp** ($N_A = 539, N_B = 423$ trades across 417 distinct sessions).
* **Verdict:** **`FAIL`**. Net P&L after real spreads and fees is negative in both halves:
  * **Half A:** **-14.56 bp** (SE: 13.99 bp, $t = -1.04$)
  * **Half B:** **-52.88 bp** (SE: 20.74 bp, $t = -2.55$)
* **Adversarial Timing Null:** The first-pullback trigger underperformed a random minute entry on the same catalyst stock by **-30.85 bp** in Half A and **-33.08 bp** in Half B.
* **Conclusion:** The common retail trading hypothesis that "buying the first pullback on catalyst gappers" provides safe asymmetric upside is empirically false. Morning gappers that pull back towards VWAP after 09:40 ET are undergoing institutional profit-taking and distribution, suffering persistent intraday fade.

---

### Quantitative Performance Breakdown

#### 1. Primary Setup vs. Adversarial Controls (Calendar-Day Clustered)

| Metric | Half A (2024-01-02 .. 2025-05-31) | Half B (2025-06-01 .. 2026-10-30) | Pooled Total |
|---|---|---|---|
| **Primary Trades ($N$)** | 539 trades (229 sessions) | 423 trades (188 sessions) | 962 trades (417 sessions) |
| **Net P&L (Flat at 11:30 ET)** | **-14.56 bp** ($t = -1.04$) | **-52.88 bp** ($t = -2.55$) | **-31.40 bp** ($t = -2.38$) |
| **Timing Lift vs. Random Minute** | **-30.85 bp** | **-33.08 bp** | **-31.83 bp** |
| **Catalyst Lift vs. Technical Gappers** | **+38.09 bp** | **-12.96 bp** | **+15.63 bp** (halves disagree) |
| **Projected MDE** | 13.81 bp | 15.20 bp | 10.24 bp |
| **Floors Met** | **True** ($\ge 300$ trades, $\ge 40$ sessions) | **True** ($\ge 300$ trades, $\ge 40$ sessions) | **True** |

#### 2. Horizon Markout Breakdown (Average Per Trade)

| Horizon | Half A Gross | Half A Net (11 bp fee/spread) | Half B Gross | Half B Net (11 bp fee/spread) |
|---|---|---|---|---|
| **3-Minute Markout** | $+0.64\text{ bp}$ | **-10.36 bp** | $-7.87\text{ bp}$ | **-18.87 bp** |
| **5-Minute Markout** | $+3.94\text{ bp}$ | **-7.06 bp** | $-1.43\text{ bp}$ | **-12.43 bp** |
| **15-Minute Markout** | $-9.27\text{ bp}$ | **-20.27 bp** | $-1.07\text{ bp}$ | **-12.07 bp** |
| **30-Minute Markout** | $+1.25\text{ bp}$ | **-9.75 bp** | $-3.09\text{ bp}$ | **-14.09 bp** |
| **11:30 ET Flat** | $+9.10\text{ bp}$ | **-1.90 bp** | $-38.45\text{ bp}$ | **-49.45 bp** |

---

### Key Diagnostic Findings

1. **Gross Thrust is Erased by Fast Fades:** While pre-market catalyst gaps are large (+3% to +10%), the gross move *following* the pullback resumption is negligible ($\approx 0\text{ to }+4\text{ bp}$). Once the $10\text{--}15\text{ bp}$ morning spread is paid, every single horizon loses money.
2. **Severe Negative Timing Lift (-32 bp):** Pullback entries significantly lagged random entries on the same ticker. Why? The true morning runners never pull back all the way to VWAP after 09:40 ET—they continue trending higher. The tickers that drop back to VWAP are the weak gappers that institutions are selling into the morning liquidity. Waiting for a VWAP pullback selectively picks the losing distribution names.
3. **Decay in Recent Sessions (Half B):** In Half B (2025–2026), post-news intraday drift deteriorated sharply (-52.88 bp net loss, $t = -2.55$), echoing the findings of our previous `earnings_drift` study where recent PEAD reversed into negative territory.
