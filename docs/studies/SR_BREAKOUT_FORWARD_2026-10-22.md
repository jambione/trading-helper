# Forward S/R test on the desk's own fills: resistance overhead FAILED to matter; breakouts UNDERPOWERED (2026-10-22)

Prereg: [sr_breakout_forward_prereg.json](sr_breakout_forward_prereg.json), registered 2026-10-06 before any later
session was scored. It was due at 10 sessions (~10/19) and is scored here, late, exactly as registered. Scorer:
`tools/studies/sr_breakout_forward_score.py` (75212dd5). Data: `ai_reports/order_block_gate/heldout_fills.jsonl`. Every
paper desk buy fill from 10/05 to 10/22 (14 sessions, 613 fills) was scored by `ob_fills_daily.py`. 10/06, 10/15 and 10/22
were added today. Measures:
- **Resistance:** charted order blocks from SIP 1-minute bars, known before the fill.
- **Outcome:** the realized round trip under the desk's own exits.

| Hypothesis (predicted) | Tested group | Group minus reference | Half 1 / Half 2 | Verdict |
|---|---|---|---|---|
| H1: resistance within 0.3% above the fill hurts (negative) | 273 of 587 fills | **+0.1 bp (t 0.06)** | -1.1 / +1.9 bp | **NOT PASSED** |
| H2: extended breakout > 0.30% hurts (negative) | 10 fills | -2.5 bp (t -0.27) | +14.6 / -20.1 | UNDERPOWERED (< 30) |
| H3: clean breakout 0.10-0.30% helps (positive) | 21 fills | -1.1 bp (t -0.28) | -0.3 / -2.0 | UNDERPOWERED (< 30) |

Statistic: group minus reference, OLS with day fixed effects, day-clustered t. Halves = 10/05-10/13 and 10/14-10/22.

**What it shows.**
- **Resistance overhead does not cost the desk anything.** Fills with a resistance block within 0.3% above did the same
  as fills without one: +0.1 bp, with a 95% range of about -3 to +3 bp. That is close to a firm zero, not a weak signal.
- **The 10/05 in-sample lead did not hold.** That lead was -17.9 bp in one half. On new sessions it is a small -5.2 bp
  over the first 2 days only, then about 0 (np-cut period -0.7 bp) and +3.8 bp (period after the cut was switched off).
- **Breakouts are too rare to judge.** Only 31 of 613 fills came after a recent breakout (10 extended, 21 clean). Their
  point estimates flip sign between halves.

**Scope.**
- **Fills:** these are the desk's real fills under its live exits, mostly on the $10 floor (the $20 floor started 10/21).
- **Resistance:** bar-based order blocks, not the live order book. The operator's Level-2 walls idea stays untested
  (BOOK_WALLS_COUNT_2026-10-22.md).
- **Today's KSS trade:** the loser that stalled under resistance 0.26% above is one fill in the flagged group. Across
  273 such fills, the pattern is not there.
