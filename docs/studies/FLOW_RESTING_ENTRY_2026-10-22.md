# Flow-conditioned resting entries: FAIL (2026-10-22)

Prereg: [flow_resting_entry_prereg.json](flow_resting_entry_prereg.json) (amended, amended_2, before the score). Script:
`tools/studies/flow_resting_entry.py` at 87fb8a93 (skeptic round 2 READY). Raw: `ai_reports/flow_resting/result.json` on
the mini.

**The question.** On strong buy flow (order-flow pilot G1), a buy limit rests at the bid for 30 s. It is filled only when
round-lot SIP prints trade strictly below it (back of the queue), held 180 s and sold at the bid. Does that make money,
and does strong flow cut the adverse selection that sank plain resting entries on 10/3?

The data is 3,614 spread-capped pre-square entries (<= 0.05%) with SIP trades fetched for every one, 0 failures.

**Verdict: FAIL in both halves.**

| | Half A | Half B |
|---|---|---|
| Strong-flow signals / fills | 410 / 114 | 592 / 158 |
| Fill rate (bar 30%) | 28% | 27% |
| **Per fill, net** (bar >= +5 bp) | **-8.6 bp** (t -4.7) | **-1.3 bp** (t -1.1) |
| Per signal (cancelled = 0) | -2.1 bp (t -4.4) | -0.7 bp (t -2.5) |
| Flow vs no-flow fills, spread-neutral (the adverse-selection gap) | -0.04 bp (t -0.02) | +0.7 bp (t 0.6) |
| Resting vs a market buy on the same signals | +2.6 bp | +3.1 bp |
| Fill rate without strong flow | 41% | 34% |

**What it shows.**
- **Order flow does not fix adverse selection.** Fills on strong-flow entries are no better than fills on the others
  (gap about 0 in both halves and both spread strata). A resting bid still fills mostly when price is coming down to it.
- **Flow and fills fight, as predicted.** Strong buying means fewer prints trade below the bid, so the fill rate drops
  from 34-41% to 27-28%.
- **Resting is still a loss reducer, not an edge.** It beats a market buy on the same signals by about +3 bp, as on 10/3,
  mostly by skipping trades. Every fill still loses.

**What it closes.** The cost side of the entry bar cannot be bought back passively on this feed: earning the spread
brings adverse selection that order flow does not remove. Together with the order-flow pilot (+1.5-2 bp at market), the
order-flow route is closed for both aggressive and passive entries.
