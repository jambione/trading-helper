# Momentum seeds: is there runway at the moment we would open them? (2026-09-27)

Script: `tools/studies/momentum_runway_study.py` (`fetch`, then `analyze`). Read-only; SIP 1m bars + SIP quotes (Alpaca historical).

## Data
- Seed record: 9/16-9/25 = `proposal_ledger` seed/kept (first ts per proposer, multi-tag); 9/1-9/15 = watch-book admissions
  (`shadow.jsonl` source) only — no separate seed log exists before 9/16, so early days under-count seeds.
- In band ($20-$100) only: 132 momentum name-days over 16 days (most momentum seeds are sub-$20).
- Controls: every eligible minute (09:35-15:30) on the same names after seeding ("random"); 236 non-seed name-days
  (names seeded on other days, not that day). Desk buys from `trades.jsonl` (n=62 momentum-tagged in band).
- Entry = next 1m open + half the name-day SIP spread. Runway = +X bp before -35 bp within 30 min (lows first).
  **mir50** = mirror (-50 before +35): the volatility null. end30 is net of the full spread.

## Main table (per event; bp)
| group | n | name-days | MFE15 | MFE30 | MAE pre-peak | +20/+35/+50 before -35 | mir50 | end30 net med / mean | gross mean |
|---|---|---|---|---|---|---|---|---|---|
| momentum random minute (post-seed) | 28,412 | 124 | 23.0 | 38.8 | -26.6 | 39 / 30 / 23.5 | 42.3 | -14 / -32 | +3 |
| momentum -50 cross | 556 | 109 | 26.6 | 41.4 | -27.8 | 43 / 32 / 23.4 | 40.6 | -9 / -28 | 0 |
| momentum desk buy | 62 | 28 | 43.4 | 58.4 | -21.0 | 53 / 47 / 38.7 | 38.7 | +1 / -13 | -1 |
| trending cross | 723 | 126 | 26.5 | 40.6 | -23.2 | 50 / 37 / 27.0 | 40.1 | -5 / +2 | +13 |
| research cross | 705 | 100 | 23.3 | 36.8 | -18.1 | 49 / 36 / 26.1 | 36.6 | -6 / -6 | +5 |
| movers cross | 631 | 124 | 19.5 | 32.6 | -24.5 | 40 / 30 / 22.8 | 36.9 | -10 / -24 | +3 |
| non-seed cross | 1,327 | 227 | 12.6 | 20.2 | -12.2 | 43 / 26 / 16.4 | 22.8 | -8 / -12 | -1 |
| **pre-seed minute** (before any source seeded the name) | 29,391 | 188 | 29.1 | 48.5 | -20.8 | 52 / 40 / 32.0 | 33.4 | **+7 / +22** | **+37** |

## Findings
1. No usable runway at open time. Momentum crosses match random minutes on the same names (+50-before-35: 23.4% vs 23.5%)
   and the mirror is ~2x larger (40.6%): big MFE is volatility, not direction. Gross drift is ~0; net = minus the spread.
2. The run happens before seeding. Momentum name-days: good-minute rate 34.2% before the seed vs 23.5% after;
   pre-seed minutes average +35 bp net at 30 min. Median seed 10:50 ET vs median HOD 12:17; 28% of name-days made HOD
   before the seed; at RTH seeds a median 60% of the open->HOD move was already done (16% had >= 80% done).
3. Hindsight upper bound: post-seed "good windows" (+50 before -35 in 30 min) are plentiful — median 8 per name-day
   (mean 12.4), spread evenly across the day — but short (median 2 min, 38% one minute) and 40/132 name-days have none.
   Non-seed names have good minutes 17% of the time, so windows exist largely because these names move a lot.
4. Timing of our entries: crosses in a window 23% (random minute 24%), early <=15 m 37% (32%), late 18% (15%),
   chop 10% (8%), dead name-day 12% (21%). Crosses are effectively random-timed. Desk buys 39% in-window (n=62, small).
5. Observable before good-window starts (vs non-good minutes): deeper 15-min pullback (70 vs 32 bp), last minute red
   (-15 bp), above VWAP (+46 vs +11 bp), newer high (66 vs 111 min), bigger day change (5.2 vs 3.4%). %R level no different.
   These mark volatility; the mirror rises with them.
6. Rule search (228 single/pair thresholds, train on alternate days, test on the rest; events with 15-min refractory):
   nothing beats base out of sample on net. Best-looking: spread < ~15 bp — test +50-before-35 24.8% vs base 23.0%,
   net mean -4.5 vs -27.8 bp (cost avoidance, not runway). `spread<21.8 & vwap>+235bp`: 35% test but n=80/13 names,
   mirror 50%, net -11. No timing rule found.
7. Context splits (momentum crosses vs random): at HOD (<25 bp) MFE30 only 12.6 bp; >150 bp below HOD worst (net mean -43).
   Weak, small-n pockets: day change 6-10% (n=77, 26 name-days, +50 32.5% vs 27.0%, net mean -1); rangepos >= 0.95
   (n=50, 36% vs 25%, net +5). Neither has been validated out of sample.
