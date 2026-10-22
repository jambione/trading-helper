# Fair-value cheapness as the entry trigger: FAIL (2026-10-22)

Prereg: [fair_value_trigger_prereg.json](fair_value_trigger_prereg.json), with amendments amended .. amended_5, all
registered before any trigger outcome was read. Script: `tools/studies/fair_value_trigger.py` at 03dd8f5f. The skeptic
reviewed the code twice; round 2's two items were applied as amended_5. Raw output: `ai_reports/fv_trigger/result.json`
and `placebo.json` on the mini.

**The question.** A name's fair value comes from SPY and its 10 closest peers. When the name turns CHEAP against that
value (its 30-minute residual Z crosses below -1), buy at the next bar and hold 3 minutes. Does that beat two controls
by at least +5 bp: the same name at a random later minute (d), and a random other name at the same minute (x)?
- **P2:** the 400-name book-like sample over 110 sessions.
- **P1:** the desk's liquid board names from 9/08 to 10/15, capped at a 0.05% spread.

**Verdict: P2 FAIL, P1 UNDERPOWERED.**

| | P2 half A | P2 half B | P1 half A | P1 half B |
|---|---|---|---|---|
| Triggers / days | 38,731 / 55 | 35,308 / 55 | 64 / 10 | 298 / 14 |
| lift vs d, placebo-adjusted (the registered rule) | -0.5 bp (t -1.7) | **-0.7 bp (t -2.9)** | +7.8 bp (t 1.3) | +4.3 bp (t 1.2) |
| lift vs d, raw (information) | +0.4 bp (t 1.7) | +0.1 bp (t 0.6) | +3.5 bp (t 0.7) | +1.9 bp (t 0.9) |
| lift vs x (another name, same minute) | +0.4 bp (t 2.2) | +0.2 bp (t 1.1) | +10.7 bp (t 1.5) | +2.9 bp (t 1.3) |
| 97.5% upper bound, d / x | +0.1 / +0.8 bp | -0.2 / +0.5 bp | | |
| Net per trade after 5 bp | **-4.7 bp** | **-4.8 bp** | -5.4 bp | -3.4 bp |
| Half verdict | NOT_PASSED | FAIL | UNDERPOWERED | UNDERPOWERED |

**Why P2 fails on any reading.** Both FAIL routes apply:
- Half B's adjusted d lift has t ≤ -2.
- Each half's upper bound on d and on x is below +5 bp.

The upper-bound route holds without the adjustment as well. Raw d and x are at most +0.4 bp, with upper bounds under
+1 bp. Cheapness is worth at most a fraction of a basis point over 3 minutes, against a 5 bp cost and a +11 bp
break-even. Gross per trade is about +0.3 bp.

**P1.** Only 64 and 298 board triggers survive the spread cap (floor 300 per half), so P1 cannot pass. Its point
estimates are positive but noisy (t ≤ 1.5, below even the 1.96 LEAD bar). P1's own cross-name placebo also failed
(pseudo lift_x t -2.6 in half B), so a P1 PASS would have been UNPROVEN anyway. Net is still negative in both halves.

**The control problem found on the way (how to read the placebos).**
- **amended_2 placebo.** Random minutes near each trigger looked about 1 bp worse than the later-minute control. Part
  of that was contamination: pseudo minutes that sat on the trigger's own falling bars.
- **amended_4/5 placebo.** Pseudo minutes were kept more than 15 minutes from every onset of the name. They now come out
  **+0.9 / +0.8 bp better** than the later minute (t 4.3 / 5.0), on both sides of the trigger.
- **Unconditional placebo.** Random other names away from any onset show +0.4 / +0.2 bp.
- **What this means.** On these name-days an earlier minute tends to beat a later one: the same intraday fade the
  universe shows at every horizon. The same-name later-minute control therefore flatters any entry by about 1 bp. The
  adjustment removes that, which is why the adjusted lift sits below the raw one.
- **Two caveats.** The size of the first placebo's bias was known when amended_3 was registered. A pseudo before its
  trigger can draw a d control on the trigger's own minutes. Neither changes the verdict, because the upper-bound
  route fails raw and adjusted alike.
- **x stays clean.** The P2 pseudo lift_x is -0.2 / +0.2 bp (t -1.0 / +1.2), inside the bound. The x control is not
  biased.

**What it closes.** Fair-value cheapness does not time entries on these names at 3 minutes, as a filter (UNDERPOWERED,
10/22) or as the trigger (FAIL). Cheap names drift back by well under 1 bp. Information cells (order flow, RICH,
Z ≤ -2, K, window, hour, IEX, other holds) were not run and are now post-hoc only.
