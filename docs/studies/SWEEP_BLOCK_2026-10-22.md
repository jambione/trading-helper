# Sweeps and blocks on the tape: FAIL (2026-10-22)

Prereg: [sweep_block_prereg.json](sweep_block_prereg.json). Amendments: amended and amended_2 (stage 1); stage2,
stage2_amended and stage2_amended_2 (combinations). All were registered before any outcome existed. Scripts:
`tools/studies/sweep_block.py` (skeptic READY at f523245a) and `tools/studies/sweep_block_stage2.py`. Data: 16,000 fresh
desk-style PRE onsets, all at least $20 and none overlapping the order-flow pilot, May-October 2026. The minute of SIP trades
and quotes before each entry, and the 3-minute mid outcome, come from the data-only account. Raw: `ai_reports/sweep_block/` on
the mini.

**The question (operator 10/22).** The tape shows real urgency where Level 2 shows orders that can be pulled. In the 60
seconds before an entry, does either kind of urgent buying predict a better 3 minutes by at least +5 bp, beyond plain
order flow?
- **H1, sweeps:** net buy intermarket-sweep volume (buyers taking every venue's ask at once).
- **H2, blocks:** net buy prints of $100k or more. 85% of these are off-exchange, so H2 is in practice a dark-pool test.

**Verdict: both FAIL.** The 97.5% upper bound is below +5 bp in both halves. The test was well powered: the count step
projected a detectable effect of 1.4-3.3 bp.

| | H1 sweeps, half A | H1 half B | H2 blocks, half A | H2 half B |
|---|---|---|---|---|
| Entries in the group | 2,497 | 2,412 | 669 | 611 |
| **Lift beyond plain flow / within notional** (primary) | **-0.2 bp** (t -0.3) | **-0.1 bp** (t -0.2) | **+1.2 bp** (t 0.9) | **+0.6 bp** (t 0.8) |
| 97.5% upper bound | +1.2 | +1.0 | +3.8 | +2.2 |
| Raw lift, no strata (information) | +0.6 | +0.7 | +1.4 | +1.0 |
| Net after the entry's own spread | -15.5 | -13.0 | -12.5 | -10.0 |
| Buy blocks vs sell blocks (information) | | | +2.1 | +1.4 |

**What it shows.**
- **Sweeps add nothing beyond plain order flow.** The raw sweep lift (+0.6 / +0.7 bp) is just the known order-flow
  signal. Comparing sweep entries with others at the same overall flow level leaves about 0 (-0.2 / -0.1 bp). Sweeps
  are about a quarter of all trades and carry no separate information.
- **Dark-pool buy blocks are slightly positive but far too small.** The lift is +0.6 to +1.2 bp, and buy blocks beat
  sell blocks by +1.4 to +2.1 bp. Even the best plausible value (+3.8 bp at the upper bound) is under the bar, and every
  group loses 10-15 bp after crossing its spread on this sample.

**Combinations (stage 2).** Only blocks passed the gate (lift above 0 in both halves). Each cell had to beat blocks
alone and the condition alone by at least 5 bp each.

| Cell | Half A | Half B | Verdict |
|---|---|---|---|
| C1 block buying that takes out the day's high | +2.0 bp (33 entries) | +5.8 bp (35) | UNDERPOWERED: too rare (3.6%) |
| C2 above VWAP on an up day | +3.3 bp (t 1.9, 439) | +0.7 bp (387) | NOT PASSED in both halves; half A cannot rule out 5 bp |
| C3 held above the early block prints into the entry | +1.2 bp (315) | +0.8 bp (281) | FAIL |

No combination passes. The one interesting read is C2 half A (+3.3 bp), and it fades to +0.7 bp in half B.

**What it closes.** Every free tape and book source has now been measured as an entry signal on this desk's entries:
| Source | Result |
|---|---|
| Plain flow | +1.5-2 bp, real but small |
| Book imbalance | ~0 |
| Sweeps | 0 beyond flow |
| Dark-pool blocks | +1 bp |
| Resting / passive entries | fails on adverse selection |
| Bar-based resistance on real fills | 0 |

Nothing approaches the +5-11 bp the exits need. The live-book walls idea is still untested (parked; it needs a depth
recording).

**Notes.**
- **Feed:** SIP features; the live desk is IEX. A pass would have needed real-time SIP, so none is needed now.
- **Order-flow strata:** these still include late off-exchange prints. That is conservative for both hypotheses and is
  declared.
- **Alpaca budget:** the desk logged 0 rate-limit errors through the data-account fetch.
