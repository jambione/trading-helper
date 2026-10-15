# Catch-up ratchet (time-decay leash) sweep — 2026-10-14

Prereg: `docs/studies/ratchet_sweep_prereg.json`. Replays of the live code (`scripts/ratchet_sweep.sh`, sha d042411) on 2026-10-06/07/08/09/12/13, live config of 94f2576 pinned, only the leash varied. Net = replay return − full SIP spread at entry. VALUE = sum of net bp per day.

| arm | opens/day | concurrency | win % | median hold | net bp/trade (t) | value bp/day | vs base, bp/day (t) |
|---|---:|---:|---:|---:|---:|---:|---:|
| base (4 s idle, overtake) | 40.0 | 0.24 | 34% | 66 s | −3.9 (−4.6) | −130.6 | — |
| ot_off | 37.5 | 0.26 | 33% | 120 s | −4.4 | −114.8 | +15.8 (0.5) |
| **idle15** | 36.3 | 0.31 | **44%** | 136 s | **−2.9** | **−72.2** | **+58.3 (2.13)** |
| idle30 | 32.5 | 0.43 | 41% | 180 s | −4.4 | −106.3 | +24.3 (0.8) |
| idle60 | 29.7 | 0.63 | 41% | 294 s | −4.6 | −85.5 | +45.0 (0.8) |
| cush10 | 34.8 | 0.55 | 28% | 181 s | −4.1 | −98.4 | +32.1 (0.9) |
| cush25 | 29.0 | 0.94 | 25% | 368 s | −6.4 | −134.3 | −3.8 (−0.1) |
| cush25i30 | 27.5 | 1.08 | 24% | 648 s | −6.8 | −136.8 | −6.2 (−0.1) |
| nodecay (leash off) | 26.7 | 1.24 | 23% | 1006 s | −7.9 | −160.9 | −30.4 (−0.6) |

idle15 minus base per day: +118.7, 0.0, +156.4, +63.7, −3.0, +14.3 bp (worse on 1 of 6 days).

**Pre-registered pick: idle15 qualifies** (paired value > 0 with t ≥ 2, worse on ≤ 1 of 6 days) for a live paper A/B. It still loses per trade (−2.9 bp), 25% less than the live leash. Giving the trade "more room" with a percent cushion did not help: win rate falls (28-24%) and losses grow; turning the leash off is worst. The useful change is patience before stepping (15 s instead of 4 s), not distance under the price.

Caveats: 6 days, replay recall vs live ~0.55-0.6; t 2.13 is close to the line; not a pass for profit — the book still loses about 3 bp a trade.
