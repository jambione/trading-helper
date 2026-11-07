# Test standard (operator 11/07)

Why this exists: every entry study had to be profitable on its own after cost, so real but small edges were filed as
"FAIL" and thrown away, and a few rules (drop the best days and names, every control at the family t-bar) were vetoes
that cut against right-tail momentum. This standard keeps the protections against luck and stops discarding real edges.

## Two verdicts for every entry study

| verdict | meaning | bar |
|---|---|---|
| **EDGE** | the signal beats a fair random minute | primary control lift > 0 in BOTH halves (by date) with day-clustered t >= the family critical value; every other control points the same way (lift > 0 pooled); survives the winsorized check |
| **PROFITABLE** | the signal makes money on its own | EDGE, plus net > 0 after realistic cost in BOTH halves, and > 0 at the stress cost |

A result can be EDGE and not PROFITABLE. That is a **component**: it is kept on the component list in
`docs/EXPERIMENTS.md`, not filed as a failure.

## What stays strict (these protect against luck)

- Pre-registration before results; one read, no retuning.
- Both halves by date; day-clustered t, never trade-level t; family-wise alpha when several hypotheses share a run.
- Controls (P2): same-name controls strictly AFTER the trade (t+hold+2 .. t+hold+62); a same-minute control on other
  stocks; each control's mean printed beside the unconditional random-minute mean, and a mismatch blocks the verdict.
- Code-review gate (P4) before a tool's first result counts; a smoke check that the tested code path fires.

## What changed

| was | now | why |
|---|---|---|
| net > 0 required for every entry study | EDGE and PROFITABLE judged separately | a +2 bp edge can never pay a 6 bp cost alone but can be combined |
| drop the best 5 days AND best 5 names: veto | REPORTED; the veto is winsorizing the top 1% of trades | momentum profit lives in the right tail |
| every control at the family t-bar | the primary control at the bar; the others only need to point the same way | one control at t 1.9 sank otherwise strong results |
| "FAIL" for anything not profitable | FAIL only when there is no EDGE either | keeps the record honest about what was found |

## Combining components

Components are combined only in a pre-registered stack with a fixed weighting chosen before results, judged on a
held-out period chosen before looking (a combined score once passed in-sample and failed out of period). A stack is
tested first where cost is low (registry B1/B2).

## Exit changes

Exit studies are judged on IMPROVES (variant beats baseline: both halves, day-t >= 2.0, winsorized check, bootstrap p5 > 0,
1 s delay on the new execution step) and separately on whether the book becomes profitable. See docs/SIM_STAGE.md.

## Amendment 11/07 12:30 (after the H2 rerun; applies to FUTURE runs only, logged as registry P5-style change)

The same-name later-only control is biased when the trigger itself predicts drift (after a stop-run the name keeps fading: H2's later-only control sat 3.6 bp below the random-minute baseline). So the PRIMARY control for EDGE is now the same-minute control on other universe stocks; the same-name later-only control is secondary (must point the same way), and its mean is still printed beside the baseline. No past verdict changes: H2 shows no EDGE against either reading of the rule (same-minute lift -0.2).
