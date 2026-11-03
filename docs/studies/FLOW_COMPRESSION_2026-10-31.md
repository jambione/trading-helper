# Study Report: Order Flow Breakout with Spread Compression (`flow_compression`)

**Date:** 2026-10-31  
**Status:** CLOSED — Verdict: `FAIL` (High Confidence, 2026 Warehouse Tape)  
**Pre-Registration:** [`docs/studies/flow_compression_prereg.json`](flow_compression_prereg.json)  
**Artifacts:** `ai_reports/flow_compression/` (`candidates.json`, `count.json`, `result.json`)  

---

### Executive Summary

We evaluated Row 58a: whether conditioning buy-stop breakouts on **spread compression to the minimum penny tick** ($\le 2.85\text{ bp}$) combined with **Lee-Ready buyer order flow imbalance ($\ge 65\%$)** overcomes execution friction to generate $\ge +3.0\text{ bp}$ net profit.

* **Sample & Universe:** Evaluated 11,017 real millisecond SIP executions from the 120 warehouse sessions ($2026-04-27\text{ to }2026-10-15$) on liquid stocks ($P \ge \$35.09$, $\text{ADV20} \ge \$20\text{M}$) with real millisecond SIP NBBO quotes at entry and exit.
* **Spread Compression Effect (The Good News):** Spread compression is a **phenomenal cost reducer (+13.38 bp lift)**. Entering when the quoted spread is compressed to $\$0.01$ cuts average losses from **-16.06 bp** on uncompressed tape down to **-2.68 bp** ($t = 15.2$).
* **Verdict:** **`FAIL`**. Despite the huge +13.4 bp compression benefit, the strategy failed to reach positive net territory:
  * **Half A:** **-2.07 bp** ($N = 275$, SE: 1.09 bp, $t = -1.90$)
  * **Half B:** **-3.13 bp** ($N = 377$, SE: 0.83 bp, $t = -3.78$)
  * **Pooled:** **-2.68 bp** ($N = 652$, SE: 0.66 bp, $t = -4.04$)
* **Core Microstructure Mechanism:**
  * Entry spread is compressed to 1 penny ($1.4\text{ bp}$ half-spread).
  * However, upon exit at the markout horizon, the quoted spread has reverted to normal market levels ($8\text{--}12\text{ bp}$, or $4\text{--}6\text{ bp}$ half-spread), plus $1.0\text{ bp}$ broker fee.
  * Total round-trip execution cost is **$7.2\text{ bp}$**.
  * The gross momentum drive of the buy-stop breakout + order flow is **$+4.5\text{ to }+4.8\text{ bp}$**.
  * Result: $+4.8\text{ bp (gross)} - 7.2\text{ bp (costs)} = \mathbf{-2.4\text{ to }-2.7\text{ bp net}}$.

---

### Quantitative Performance Breakdown

#### 1. Spread Compression Contrast (Real SIP Executions, $P \ge \$35.09$)

| Regime | Condition | Trades ($N$) | Mean Net P&L | $t$-statistic |
|---|---|---|---|---|
| **Uncompressed Tape** | Spread $> \$0.01$ (All Flows) | 10,365 | **-16.06 bp** | -38.48 |
| **Uncompressed Tape** | Spread $> \$0.01$ + Buyer Flow ($\text{OFI} \ge 0.20$) | 4,012 | **-16.20 bp** | -24.53 |
| **Uncompressed Tape** | Spread $> \$0.01$ + Strong Buy ($\text{OFI} \ge 0.50$) | 1,945 | **-16.96 bp** | -17.42 |
| **Compressed Tape** | Spread $= \$0.01$ (All Flows) | 652 | **-2.68 bp** | -4.04 |
| **Compressed Tape** | Spread $= \$0.01$ + Buyer Flow ($\text{OFI} \ge 0.20$) | 285 | **-2.24 bp** | -2.45 |
| **Compressed Tape** | Spread $= \$0.01$ + Strong Buy ($\text{OFI} \ge 0.50$) | 94 | **-2.92 bp** | -2.13 |

**Compression Lift:** $-2.68\text{ bp} - (-16.06\text{ bp}) = \mathbf{+13.38\text{ bp}}$ ($t > 15.0$).

#### 2. By-Half Chronological Breakdown (Primary Compressed Arm)

| Half | Window | Trades ($N$) | Net P&L | SE | $t$-statistic | Floors Met |
|---|---|---|---|---|---|---|
| **Half A** | 2026-04-27 .. 2026-07-29 | 275 | **-2.07 bp** | 1.09 bp | -1.90 | **True** |
| **Half B** | 2026-07-30 .. 2026-10-15 | 377 | **-3.13 bp** | 0.83 bp | -3.78 | **True** |
| **Pooled** | 2026-04-27 .. 2026-10-15 | 652 | **-2.68 bp** | 0.66 bp | -4.04 | **True** |

---

### Diagnostic Conclusions

1. **Why It Failed the Net Hurdle:**
   Spread compression at entry is real and provides a $+13.4\text{ bp}$ lift. However, a round-trip trade cannot avoid the exit spread. Because the exit fills at an uncompressed bid, the trader pays the full exit half-spread ($4\text{--}6\text{ bp}$) plus broker friction ($1\text{ bp}$).
2. **Gross Edge is Real but Capped at +4.8 bp:**
   The buy-stop breakout combined with buyer order flow does generate genuine gross forward lift ($+4.8\text{ bp}$). But $+4.8\text{ bp}$ gross is mathematically insufficient to absorb a $7.2\text{ bp}$ real execution hurdle.
3. **Implications for Live Trading:**
   Spread compression should be maintained as an **entry filter** to eliminate wide-spread trades (saving 13 bp), but it cannot turn a sub-5 bp gross momentum signal into a standalone profitable system on market/touch orders.
