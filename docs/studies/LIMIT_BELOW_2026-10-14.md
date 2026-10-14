# Limit below the signal - FAIL (2026-10-14)

Prereg: `docs/studies/limit_below_prereg.json` (result-skeptic READY after 2 rounds, before any score).
Script: `tools/studies/limit_below_study.py`; tests `tests/test_limit_below_study.py`. 26 cached sessions, 566 pre-square signals, cache only.

## Question
Operator 10/14: every fill dips (94% of October fills went below the fill; 36% by 10+ bp). Rest a buy limit 10 bp below the signal's IEX price for 5 minutes instead of buying at market - does the dip give a better entry?

## Verdict: FAIL in both halves
| | half A 9/08-9/24 | half B 9/25-10/13 |
|---|---:|---:|
| fill rate | 73% | 43% |
| filled trades net (10 bp) | -51.5 bp (t -2.0) | -14.9 bp (t -3.0) |
| all signals at market net | -26.6 bp | -13.8 bp |
| lift (fills - market) | -22.1 bp (t -2.1) | -1.1 bp |

## Why: adverse selection
Market result of signals the limit FILLED vs did NOT fill: half A -65.6 vs +57.7 bp; half B -22.6 vs -3.8 bp. The dip after a signal is usually the start of the fall (most fills come in the first minute: 101 of 144 in half A); the names that run never come back to fill. Every grid cell (5/10/20 bp x 2/5/10 min) and the 5 bp cost variant tell the same story. The control (same limit at random later minutes) also loses (-29.6 / -36.3 bp).

## What it suggests (a hypothesis, not a result)
The first minutes AFTER the signal sort winners from losers: signals that held up did far better than those that dipped. The inverse entry - wait N minutes, buy only if the price has not dipped - is knowable at the time and is the next prereg. Its cost is the higher price paid by waiting.

## Raw output
# Limit below the signal - primary k=10 bp, N=5 min

signals 566

| half | signals | fills | fill rate | fill net | market net (all signals) | lift | t | verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| A | 198 | 144 | 73% | -51.5 | -26.6 | -22.1 | -2.07 | FAIL |
| B | 361 | 155 | 43% | -14.9 | -13.8 | -1.1 | -0.52 | FAIL |

**Limit below (10 bp, 5 min, IEX reference): FAIL**

Filled-trade net t per half: A -2.00; B -2.98

### Information only

- CONTROL (same limit at a random later minute of the same name and day): half A fill net -29.6 (fill rate 76%, SIP-reference fallbacks 2); half B fill net -36.3 (fill rate 45%, SIP-reference fallbacks 2) - if control fill net >= the signal's, the result is a fill-model artifact (forward test only)
- half A: limit fills exited at the MARKET arm's exit bar: -49.6 (price gain over the same window)
- half B: limit fills exited at the MARKET arm's exit bar: -14.8 (price gain over the same window)
- SIP-close reference (not available live): half A fill net -49.8, lift -20.5; half B fill net -18.5, lift -4.7
- 5 bp trade-through: half A fill net -65.7, lift -36.3; half B fill net -17.0, lift -3.2
- skipped name-days: {'name_day_no_or_short_bars': 107}
- half A: market net of signals the limit FILLED -65.6 vs NOT filled +57.7 (adverse selection); per-signal value with unfilled = 0: -33.1; fill minute after signal {1: 101, 2: 21, 3: 13, 4: 5, 5: 4}; filled with no exit bar at all 0
- half B: market net of signals the limit FILLED -22.6 vs NOT filled -3.8 (adverse selection); per-signal value with unfilled = 0: -7.9; fill minute after signal {1: 72, 2: 39, 3: 16, 4: 13, 5: 15}; filled with no exit bar at all 0

| k bp | N min | half | fill rate | fill net | lift | t |
|---:|---:|---|---:|---:|---:|---:|
| 5 | 2 | A | 75% | -43.8 | -17.1 | -1.91 |
| 5 | 2 | B | 43% | -20.4 | -6.6 | -1.52 |
| 5 | 5 | A | 84% | -40.5 | -13.8 | -1.56 |
| 5 | 5 | B | 59% | -16.4 | -2.6 | -1.83 |
| 5 | 10 | A | 88% | -39.6 | -13.0 | -1.47 |
| 5 | 10 | B | 71% | -16.3 | -2.5 | -1.88 |
| 10 | 2 | A | 62% | -54.9 | -25.5 | -2.08 |
| 10 | 2 | B | 31% | -16.6 | -2.8 | -0.49 |
| 10 | 5 | A | 73% | -51.5 | -22.1 | -2.07 |
| 10 | 5 | B | 43% | -14.9 | -1.1 | -0.52 |
| 10 | 10 | A | 78% | -48.2 | -18.8 | -1.89 |
| 10 | 10 | B | 58% | -15.2 | -1.4 | -1.01 |
| 20 | 2 | A | 47% | -81.3 | -51.9 | -3.97 |
| 20 | 2 | B | 14% | +2.2 | +16.4 | +0.59 |
| 20 | 5 | A | 59% | -72.2 | -42.8 | -3.44 |
| 20 | 5 | B | 25% | -10.7 | +3.1 | +0.34 |
| 20 | 10 | A | 66% | -66.9 | -37.5 | -3.21 |
| 20 | 10 | B | 37% | -15.2 | -1.4 | -0.37 |

- primary with the limit charged 5 bp: half A fill net -46.5, lift -17.1; half B fill net -9.9, lift +3.9
