# Honest early entry — FAIL (2026-10-13)

Prereg: `docs/studies/early_entry_honest_prereg.json` (result-skeptic READY after 2 rounds, before any score).
Script: `tools/studies/early_entry_honest.py`. Tests: `tests/test_early_entry_honest.py`.

## Question
AGY's `early_entry_sim.py` showed that buying the desk's own October fills 5 minutes earlier would have turned −$121 into +$174, and 10 minutes earlier +$420. That was used to justify pre-square (6bae9f8), premarket seating (577d3cb) and the 09:45 start (689dfca). The shift assumes we knew at M−5 that the signal was coming. Here, every rule reads only completed bars and fires on every board name.

## Verdict
**PRE (pre-square, bar-close approximation): FAIL. IGN (ignition): FAIL.** Both halves are powered (PRE 198/361 signals, IGN 709/446, 13 days each).

| rule | half | n | gross | net (10 bp) | lift vs later same-name minute | t |
|---|---|---:|---:|---:|---:|---:|
| PRE | A 9/08–9/24 | 198 | −16.6 | −26.6 | −5.8 | −0.25 |
| PRE | B 9/25–10/13 | 361 | −3.8 | −13.8 | −5.9 | −0.63 |
| IGN | A | 709 | −18.4 | −28.4 | −5.4 | −0.32 |
| IGN | B | 446 | +10.8 | +0.8 | +23.8 | +1.35 |
| SQ (info) | A / B | 276 / 362 | −11.8 / −11.3 | −21.8 / −21.3 | −13.3 / −5.9 | — |

## What the information lines say
- **Pre-square does not fire 3–5 minutes before our fills.** Only 276 of 1,057 desk fills had a PRE fire in the prior 15 minutes, with a median lead of 1 minute. IGN leads by a median of 4 minutes (61% lead by ≥ 3 min) but only precedes 206 fills, and it loses money on its own.
- **The board itself fades:** every board minute averages −8.3 bp gross over 10 minutes (t −2.8, n 73k). Rules and controls swim in the same negative tide (see the-universe-fades-at-every-horizon).
- **AGY's method on this data:** shift-5 on random rising board minutes with no rule at all scores **+95.5 bp (t 8.9)**. On a synthetic random walk it scores +44.5 bp (t 53). The +$294 came from the method, not the market.
- Costs and timing: net at 20 bp is −24 to −32 bp for all three; entering at the close instead of the open changes nothing.

## Method note: same-hour controls are biased for momentum rules
On a driftless synthetic walk (30 days × 15 names), a same-name same-hour random control scored PRE −12, IGN −7, SQ −21 bp (|t| 2.3–7.3). An earlier window in the hour can sit on the run-up the rule fired after. The later-only control (entry+1..+60) scored all three within ±3.5 bp. `latch_gate_sim.py` and other same-hour-control studies carry this bias for any rule that fires after a rise.

## Raw output
# Honest early entry — 2026-09-08 .. 2026-10-13

days 26, half A ends 2026-09-24. Cost 10 bp RT, hold 10 min.

Scored signals per half: PRE A 198 / B 361, IGN A 709 / B 446, SQ A 276 / B 362

| rule | half | n | days | gross | net | lift | t | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| PRE | A | 198 | 13 | -16.6 bp | -26.6 bp | -5.8 bp | -0.25 | FAIL |
| PRE | B | 361 | 13 | -3.8 bp | -13.8 bp | -5.9 bp | -0.63 | FAIL |
| IGN | A | 709 | 13 | -18.4 bp | -28.4 bp | -5.4 bp | -0.32 | FAIL |
| IGN | B | 446 | 13 | +10.8 bp | +0.8 bp | +23.8 bp | +1.35 | FAIL |
| SQ | A | 276 | 13 | -11.8 bp | -21.8 bp | -13.3 bp | -0.48 | FAIL |
| SQ | B | 362 | 13 | -11.3 bp | -21.3 bp | -5.9 bp | -1.11 | FAIL |

**Verdicts:** PRE FAIL, IGN FAIL, SQ INFO

### Information only

Lift at other holds (day-equal, t):
- PRE: 5m +8.4 bp (t +0.64, n 557); 15m +8.7 bp (t +0.54, n 557); 30m -4.1 bp (t -0.31, n 556)
- IGN: 5m +10.8 bp (t +1.37, n 1136); 15m +1.9 bp (t +0.13, n 1135); 30m -28.2 bp (t -2.08, n 1133)
- SQ: 5m -12.1 bp (t -1.09, n 632); 15m -23.1 bp (t -1.45, n 633); 30m -32.0 bp (t -1.09, n 627)

Lead before desk fills (latest fire within 15 min):
- PRE: 276/1057 fills had a prior fire; median lead 1 min; lead >= 3 min 42%; 566 fires, 37% followed by a desk fill within 10 min
- IGN: 206/1057 fills had a prior fire; median lead 4 min; lead >= 3 min 61%; 1199 fires, 13% followed by a desk fill within 10 min
- SQ: 197/1057 fills had a prior fire; median lead 2 min; lead >= 3 min 47%; 654 fires, 23% followed by a desk fill within 10 min

Other controls and costs (lift unless noted; day-equal, t, n):
- PRE: cross -28.6 bp (t -1.83, n 556); close_entry -4.3 bp (t -0.38, n 559); net20 -30.2 bp (t -3.47, n 559); later-control overlaps the hold 14%
- IGN: cross -3.5 bp (t -0.22, n 1148); close_entry +0.3 bp (t +0.02, n 1155); net20 -23.8 bp (t -1.75, n 1155); later-control overlaps the hold 17%
- SQ: cross -5.6 bp (t -0.27, n 635); close_entry -4.9 bp (t -0.35, n 638); net20 -31.5 bp (t -2.95, n 638); later-control overlaps the hold 15%
- every board minute, gross 10m: -8.3 bp (t -2.77, n 73031)

Artifact demo (shift-5 lift on rule-free rising board minutes): +95.5 bp (t +8.93, n 1335)

Drops: {'PRE_off_board': 3178, 'IGN_off_board': 9348, 'SQ_off_board': 5539, 'no_bars': 107, 'SQ_no_bar': 16, 'PRE_no_bar': 7, 'IGN_no_bar': 44}
