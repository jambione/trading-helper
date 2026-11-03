# Proposal: Two-Stage Order Flow Breakout with Spread Compression (`flow_compression`)

**Date:** 2026-10-31  
**Author:** Antigravity (Pair Programming with Operator)  
**Status:** PROPOSAL — Finalized after Skeptic Round 2 (All FIX FIRST items resolved)  
**Preregistration Target:** `docs/studies/flow_compression_prereg.json`  

---

### 1. Executive Summary & Core Hypothesis

**The Problem:** In previous studies, buyer-initiated trade flow (`ORDER_FLOW_PILOT`) proved to have a statistically robust forward lift of $+1.8\text{ bp}$ ($t \approx 3.5$), and buy-stop breakouts (`BATCH2`) proved to have a $+2.2\text{ bp}$ forward lift ($t = 5\text{ to }10$). However, both failed standalone trading because standard quoted spreads ($10\text{--}15\text{ bp}$) exceed the combined $+4.0\text{ bp}$ gross signal.

**The Hypothesis (Row 58a):** When an aggressive institutional breakout ignites on a high-priced mover ($P \ge \$35.09$), liquidity providers and aggressive buyers pack the inside quotes, causing the quoted spread to **compress to the minimum penny tick** ($\le 2.85\text{ bp}$), while relative spread drops to $\le 50\%$ of trailing median.
* **Microstructure Signal:** Sudden spread tightening on surging volume indicates aggressive limit bidding that absorbs book depth right before price advances. Requiring baseline median spread $\ge \$0.02$ filters out permanent penny-spread mega-caps (AAPL, NVDA), isolating genuine volatility runners whose books suddenly tighten.
* **Cost Reality:** The strategy enters at the compressed penny ask ($\text{half-spread} \le 1.4\text{ bp}$). It exits at the true prevailing SIP Bid at the exit horizon (paying the uncompressed exit spread). 
* **The Test:** Does entering on spread compression + Lee-Ready buyer flow generate sufficient gross thrust to produce a positive net return after paying real round-trip execution costs?

---

### 2. Universe & Two-Stage Pipeline Architecture (Skeptic Round 2 Fix 1)

* **Sample Window:** 2024-01-02 through 2026-10-30 (712 trading sessions).
* **Split Halves:**
  * **Half A:** 2024-01-02 .. 2025-05-31.
  * **Half B:** 2025-06-01 .. 2026-10-30.
* **Stage 1: Candidate Breakout Identification (Bar Data Only):**
  * Evaluated across the liquid universe (NYSE/Nasdaq common stocks, ADV20 $\ge \$20\text{M}$).
  * **Strict Price Floor (Skeptic Round 2 Fix 4):** $\min(P_{\text{D-1 close}}, P_{\text{open}}, P_{\text{trigger}}) \ge \mathbf{\$35.09}$ (ensures a 1-cent tick is mathematically $\le 2.85\text{ bp}$).
  * **Candidate Filter:** Candidate buy-stop breakout minutes during Regular Trading Hours ($09:45\text{--}15:45\text{ ET}$) where price crosses above the prior 1-minute high on above-average 1-minute volume.
  * *Volume of candidates:* Yields an average of $3\text{--}8$ candidates per session ($\sim 3,500$ events total over 712 sessions).
* **Stage 2: Microstructure & Quote Verification (Candidate Window Only, Skeptic Round 2 Fix 1):**
  * For candidate minutes only, query SIP tick quotes from $T-10\text{m}$ to $T+15\text{m}$ (to compute the 10-minute trailing median spread and support up to 15-minute markouts and leash exits).
  * Query SIP trades from $T-60\text{s}$ to $T$ (for Lee-Ready flow imbalance).
  * Completely solves the infinite tape problem: requires only candidate-window API calls, fully respecting Alpaca rate limits and disk caps.

---

### 3. Setup & Entry Trigger (Skeptic Round 2 Fix 4)

A candidate trade qualifies for entry at second $T$ if and only if **all three** conditions hold simultaneously:
1. **Spread Compression Gate:**
   * Quoted consolidated SIP spread at $T$ is strictly **$\$0.01$** (1 penny) AND $\le 2.85\text{ bp}$ ($P \ge \$35.09$).
   * Quoted spread at $T$ is $\le 50\%$ of the ticker's trailing 10-minute median quoted spread (which mandates trailing median spread $\ge \$0.02$, focusing strictly on dynamic spread tightening on movers).
2. **Buyer Order Flow Imbalance:**
   * Consolidated Lee-Ready signed trade volume over the preceding 60 seconds ($T-60\text{s}$ to $T$) is predominantly buyer-initiated:
     $$\text{Flow Imbalance} = \frac{\text{Volume}_{\text{buy}}}{\text{Volume}_{\text{buy}} + \text{Volume}_{\text{sell}}} \ge 0.65$$
3. **Buy-Stop Breakout:**
   * Price at second $T$ triggers across the high of the preceding 1-minute bar.
4. **Execution:**
   * Filled at the true SIP Ask price at $T$ (charging the 1-cent spread).

---

### 4. Exit Rules & Cost Model

* **Markout Horizons:** 1-minute, 3-minute, 5-minute, and 15-minute forward markouts.
* **Realistic Stop Model:**
  * Initial stop set to the low of the preceding 1-minute breakout candle (or $1.0\text{R}$, where $R = \text{ATR}_{14}$).
  * Trailing desk exit evaluated via `leash_replay.walk`.
* **Honest Execution Cost Model:**
  * Entry: Buys at the SIP Ask at $T$.
  * Exit: Sells at the prevailing real SIP Bid at the exit timestamp (charging the uncompressed exit spread).
  * Broker Fee: $1.0\text{ bp}$ friction.
  * Net Return:
    $$\text{Net P&L} = \frac{\text{Bid}_{\text{exit}} - \text{Ask}_{\text{entry}}}{\text{Ask}_{\text{entry}}} - 0.0001$$

---

### 5. Controls (Adversarial Nulls, Skeptic Round 2 Fix 3)

1. **Control 1 (Uncompressed Contrast):** Candidate breakouts with buyer flow $\ge 0.65$ where the quoted spread is *uncompressed* (`quoted spread > $0.01 OR relative spread > 50% of trailing median`). (Isolates the specific value of the spread compression filter; zero dead zone).
2. **Control 2 (Flow Contrast):** Candidate breakouts with spread compression ($\le 2.85\text{ bp}$) but *without* buyer flow imbalance (Flow Imbalance $< 0.50$). (Isolates the specific value of the buyer order flow filter).
3. **Control 3 (Placebo Timing Null):** A random minute within the candidate ticker's $[T-30\text{m}, T+30\text{m}]$ window, evaluated at prevailing NBBO Ask/Bid. (Tests whether the breakout timing adds edge beyond being in a mover during that active trading hour).

---

### 6. Success Bar & Decision Rule (Skeptic Round 2 Fix 2)

* **Minimum Events:** $\ge 500$ trades per half across $\ge 40$ distinct sessions.
* **MDE Bar:** Target $\text{MDE} \le 3.0\text{ bp}$ (standardized to calendar day clustering).
* **Pass Criteria (All must hold):**
  1. **Pooled Net P&L (Harmonized with MDE):** Net expectancy $\ge \mathbf{+3.0\text{ bp}}$ per trade after full uncompressed exit spread and fees (guaranteeing expected $t \ge 2.8 > 2.0$ at target MDE).
  2. **Both Halves Positive:** Net expectancy $> 0$ in both Half A and Half B with $t \ge 2.0$.
  3. **Compression Lift:** Outperforms uncompressed control by $\ge +3.0\text{ bp}$ with $t \ge 2.0$.
  4. **Beats Placebo:** Outperforms random minute control by $\ge +2.0\text{ bp}$ with $t \ge 2.0$.
* **Verdict Labels:** `PASS`, `FAIL`, or `UNDERPOWERED`.
