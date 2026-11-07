# Simulation stage (replaces the live forward test), operator 11/07

Decision: no waiting on live paper days. A candidate (exit change or entry gate) is vetted by simulation only, in four tiers, then the operator decides.

| tier | what | data | bar |
|---|---|---|---|
| S1 screen | sweep_replays screen (5 recorded days, real live code) | recorded sessions | mean day delta > 0 and >= 60% of days better |
| S2 confirm | sweep_replays confirm (10 more recorded days) | recorded sessions | mean > 0, day-t >= 1.5, both halves > 0 |
| S3 held-out trade simulation | every logged desk entry since 10/08 (776+ trades): real post-fill SIP prints run through the real position code (local_profit_stop, peak lock, tranche A) trade by trade | early_path.trades_since + SIP prints | variant net > baseline net in BOTH halves (by date), day-clustered t >= 2.0, > 0 after dropping the best 5 days and the 5 largest-P&L names, > 0 at 10 bp cost |
| S4 stress | block-bootstrap of S3 by day (10,000 resamples) and a shift test (apply the rule 1 s late) | S3 output | 5th percentile of the day-mean delta > 0; still > 0 with a 1 s delay |

Rules
- The same candidate must clear S1-S4 in order; a failure stops it (registry status verdict_fail with the failing tier).
- S3 excludes the 15 confirm days used by S2 from its headline only if the overlap exceeds half the sample; it reports both.
- Hold-based variants (trend_hold) need %R bars, which a price-only S3 cannot recompute. They use S1+S2 plus S3 on trades where the hold did not matter (reported separately) until bars can be replayed.
- A pass moves to the operator with the caveat that simulation is not live: fills are at recorded prints, no queue position, no market impact. Any live change is the operator's call, optionally on one slot.
- Controls: every study-style candidate follows the P2 control standard (docs/EXPERIMENTS.md, item P2).
