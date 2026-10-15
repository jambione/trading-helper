# RSI-2 red as an exit — FAIL (2026-10-14)

Prereg: `docs/studies/rsi2_red_exit_prereg.json` (skeptic two rounds). Desk trades 9/08-10/13 with holds ≥ 2 min (740 eligible); sell at the next SIP open less half the spread after a completed IEX bar with RSI-2 > 90; graded against a hazard-matched random exit.

| half | eligible | red fired | on the entry bar | red − hazard exit (t) | red vs real fill (t) | verdict |
|---|---:|---:|---:|---:|---:|---|
| A | 265 | 101 | 49% | +6.8 bp (1.86) | +4.3 (0.94) | FAIL |
| B | 475 | 181 | 73% | +2.0 bp (1.89) | +3.6 (2.28) | FAIL |

Both halves positive but under t 2; red fires mostly on the entry bar itself, and from the bar after entry it is ≈ 0 vs the null (+2.3 / +0.1). One minute later (live latency) half A +8.3 (t 2.5), half B +1.5. NBBO complete for both arms. **Closed as a completed-bar exit**; the small positive is mostly "exit right after a spiky entry bar", which overlaps the early-dip / wait-and-hold forward tests.
