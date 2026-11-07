# Simulation stage (replaces the live forward test), operator 11/07

Decision: no waiting on live paper days. A candidate (exit change or entry gate) is vetted by simulation only, in four tiers, then the operator decides.

| tier | what | data | bar |
|---|---|---|---|
| S1 screen | sweep_replays screen (5 recorded days, real live code) | recorded sessions | mean day delta > 0 and >= 60% of days better |
| S2 confirm | sweep_replays confirm (10 more recorded days) | recorded sessions | mean > 0, day-t >= 1.5, both halves > 0 |
| S3 held-out trade simulation | every logged desk entry since 10/08 (776+ trades): real post-fill SIP prints run through the real position code (local_profit_stop, peak lock, tranche A) trade by trade | early_path.trades_since + SIP prints | IMPROVES: variant minus baseline > 0 in BOTH halves (by date), day-clustered t >= 2.0, > 0 after dropping the best 5 days and the 5 largest-P&L names. PROFITABLE (separate): variant mean net > 0 at 10 bp cost. (Clarified 11/07: an improvement on a losing baseline is not a pass.) |
| S4 stress | block-bootstrap of S3 by day (10,000 resamples) and a shift test: EVERY exit 1 s late, in both arms (the rule as written at 08:25 before any result; a 11/07 rewrite after results was reverted, registry P5) | S3 output | 5th percentile of the day-mean delta > 0; still > 0 with a 1 s delay |

Rules
- The same candidate must clear S1-S4 in order; a failure stops it (registry status verdict_fail with the failing tier).
- S3 excludes the 15 confirm days used by S2 from its headline only if the overlap exceeds half the sample; it reports both.
- Hold-based variants (trend_hold) need %R bars, which a price-only S3 cannot recompute. They use S1+S2 plus S3 on trades where the hold did not matter (reported separately) until bars can be replayed.
- A pass moves to the operator with the caveat that simulation is not live: fills are at recorded prints, no queue position, no market impact. Any live change is the operator's call, optionally on one slot.
- Controls: every study-style candidate follows the P2 control standard (docs/EXPERIMENTS.md, item P2).

Simulator fidelity (11/07 audit): acts on the last print per 0.25 s shelf tick (not every SIP print); bad-tick filter is backward-only (>300 bp from the median of the previous 5 prints); after tranche A the runner keeps the local trail with a floor at the breakeven floor (entry + 1c), as live (apply_local_trail has no tranche exception; _runner_stop_level >= breakeven_floor). Known gap: no trend hold (needs bars). A tool must pass the code-review gate (registry P4) before its numbers count.

Code review 11/07 (P4, first verdict NOT YET) changed the model: the S3 verdict is read at a 0.5 s exit latency in both arms (live: 0.25 s tick + order round trip + market fill); decisions use a 0.25 s forward-filled tick with the live 8-print shelf ring; the tranche follows the live path (market at the 3 s book tick if already through T1, else a resting limit that fills once a print reaches target x (1 + th)). Canaries: planted target exit, null knob (must be VACUOUS), latency fill.
