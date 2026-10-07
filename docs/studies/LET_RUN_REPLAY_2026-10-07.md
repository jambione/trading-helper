# Let-it-run replay (G1): FAIL — every looser trail loses more per trade

Pre-registration: [`let_run_replay_prereg.json`](let_run_replay_prereg.json). Script: `scripts/let_run_replay.sh`.
Six sessions 2026-09-29..10-06, live code replayed, costed with the full SIP spread (`replay_costing.py`).

| variant | trades | /day | gross | spread | net bp/trade | t(days) | net $/day @1k |
|---|---|---|---|---|---|---|---|
| base (live) | 395 | 66 | +0.1 | 7.7 | **-7.6** | -5.25 | -49.99 |
| decay_off | 178 | 30 | +0.4 | 9.1 | -8.7 | -1.07 | -25.82 |
| decay_slow (30 s) | 333 | 56 | -0.2 | 8.0 | -8.2 | -3.87 | -45.34 |
| give_wide | 359 | 60 | -1.3 | 8.0 | -9.2 | -3.14 | -55.21 |

**Verdict (pre-registered bar: variant minus base >= +3 bp/trade): FAIL for all three.** No variant beats the live trail
per trade; the 10/5-10/6 "hold longer" look (two up days) did not generalize. Information: removing the 8 s leash halves
the trade count (fewer round trips), which halves the daily dollar loss, but each trade still loses and loses slightly
more. G1b (slot count) is moot.

```
REPLAY COSTING 2026-09-29..2026-10-06 (6 days): bp per trade, full SIP spread charged once

  variant  trades  /day   gross  spread     net  t(days)  net $/day @1k
  base        395    66    +0.1     7.7    -7.6    -5.25         -49.99
  decay_off    178    30    +0.4     9.1    -8.7    -1.07         -25.82
  decay_slow    333    56    -0.2     8.0    -8.2    -3.87         -45.34
  give_wide    359    60    -1.3     8.0    -9.2    -3.14         -55.21
```
