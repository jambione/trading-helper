# Intraday pairs reversion: UNDERPOWERED by the trade floor, negative out of sample (2026-10-19)

Prereg `docs/studies/pairs_revert_prereg.json` (two amendments, result-skeptic READY). Script `tools/studies/pairs_revert.py --score`, one run on the mini. 119 sessions, split 2026-07-23, net of 3 bp of gross notional.

**P1_declared: UNDERPOWERED | P2_chosen: UNDERPOWERED** (half B 205 and 264 trades, under the 300 floor).

| arm | half | sessions | trades | net bp (t) | net6 | excess vs placebo (t) | exits |
|---|---|---:|---:|---:|---:|---:|---|
| P1 | A | 60 | 302 | -1.5 (-0.88) | -4.5 | -1.5 (-0.22) | 60 min 288, cross 6, 15:50 8 |
| P1 | B | 55 | 205 | -3.6 (-1.75) | -6.6 | +1.5 (+0.20) | 60 min 197, cross 4 |
| P2 | A | 60 | 529 | +5.1 (+2.21) | +2.1 | -17.2 (-1.48) | 60 min 510, cross 10 |
| P2 | B | 56 | 264 | -1.2 (-0.48) | -4.2 | -13.6 (-1.00) | 60 min 254, cross 1 |

Concentration (half B, net without the best sessions / pairs / names): all negative on both arms.

Plain words: a 2-sigma intraday gap between related names does not close inside 60 minutes (cross exits are 1-2% of trades), so the trade is a coin flip that pays the cost. The data-chosen pairs are the classic overfit: positive on the months they were picked from, negative after, and below their placebo partners in both halves. The floor makes the word UNDERPOWERED; the signs say there is nothing to power. Closed. The quiet-control column (-50 to -90 bp) is information only and biased by construction: its minute is drawn from the hour of a trade, often before the divergence that triggered it.
