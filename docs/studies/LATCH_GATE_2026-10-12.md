# %R latch / RSI fire: FAIL (2026-10-12)

Pre-registration: `docs/studies/latch_gate_prereg.json` (commits `a735eaf`, `156c263`, `473ae4c`, all before the run). Result-skeptic READY on the registration. Script: `tools/studies/latch_gate_sim.py --score`. One run. No cell was changed after the number existed.

The rule scored is the one stated on 2026-10-12. Both %R lines rising open a gate. Both lines falling shut it. One line turning down leaves the gate where it was. While the gate is open, CM RSI-2 between 0 and 50 and turning up fires a buy at the next bar's open. RSI at 70 or higher sells that swing at the next open and does not shut the gate. A shut gate does not flatten an open trade.

Data: the indicator-levels SIP 1-minute cache (`ai_reports/indicator_levels/ext.pkl`), admitted name-days, extended hours so the slow line is warm, regular-hours entries only, price at least $10. 20 sessions. The cut, taken from the calendar before any return, is 2026-09-18. This is the chart tape. The live desk reads IEX. The five clips from today are not in the cache.

Cost: 20 bp round trip. Control: one random regular-hours minute in the same name and clock hour, held the same number of minutes. Pass required both halves to beat that control by at least 5 bp with day-clustered t at least 2, and the round trip itself to be above zero, with at least 100 trades and 8 days in each half.

## Result

5,476 fires. 5,475 sold because RSI reached 70. One was still open at 15:50. Median hold 4 minutes. 906 fires happened while the gate was open from an earlier bar rather than from both lines rising on that bar. On 3,232 trades the gate shut before the exit.

| half | trades | days | lift vs random | t | net after 20 bp | gross |
|---|---:|---:|---:|---:|---:|---:|
| through 2026-09-18 | 1,287 | 10 | +5.5 bp | +3.83 | −20.0 bp | +0.0 bp |
| after 2026-09-18 | 4,188 | 10 | +5.4 bp | +5.56 | −19.9 bp | +0.1 bp |

**FAIL.** The lift bar was met in both halves. The round trip was not. Before the spread the trade is flat. After 20 bp it loses the spread. A fixed 15-minute markout, which cannot pass or fail this study, was +3.8 bp (t +2.85) and +11.2 bp (t +3.95) against the same kind of random minute.

The live square arm was not changed.
