# Confirm-then-buy result 2026-10-16: CONF1 FAIL, CONF2 UNDERPOWERED (negative); not promising

Prereg docs/studies/confirm_entry_prereg.json + amended_1 (skeptic round 2, final fix dc12960, committed before scoring). Warm lines, 27 usable sessions, half A ends 2026-09-24.

Waiting for the price to hold made the entry WORSE than buying the pre-square at once: CONF1 minus PRE pooled lift -35.6 bp, CONF2 minus PRE -14.5 bp. Held signals were no better than random held minutes at k=1 (CONF1 - HELD1 -12.6 bp). Dipped signals were worse in raw return (DIP1 gross -37.6/-14.7 vs CONF1 -6.5/-8.9), consistent with the limit-below study, but the held ones are ordinary, and the later entry gives up what the filter saves. Confirm-then-buy on the pre-square is closed (if_fail).

```
# Honest early entry — 2026-09-08 .. 2026-10-15

days 28, half A ends 2026-09-25. Cost 10 bp RT, hold 10 min.

Scored signals per half: CONF1 A 96 / B 193, CONF2 A 85 / B 94, DIP1 A 104 / B 150, DIP2 A 94 / B 126, HELD1 A 101 / B 202, HELD2 A 85 / B 174, PRE A 294 / B 472, SQ A 419 / B 523

| rule | half | n | days | gross | net | lift | t | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| CONF1 | A | 96 | 13 | -6.5 bp | -16.5 bp | -50.8 bp | -1.55 | UNDERPOWERED |
| CONF1 | B | 193 | 13 | -8.9 bp | -18.9 bp | -4.5 bp | -0.48 | FAIL |
| CONF2 | A | 85 | 13 | -30.8 bp | -40.8 bp | -1.1 bp | -0.02 | UNDERPOWERED |
| CONF2 | B | 94 | 13 | -3.6 bp | -13.6 bp | -12.0 bp | -1.35 | UNDERPOWERED |
| DIP1 | A | 104 | 13 | -37.6 bp | -47.6 bp | -0.3 bp | -0.01 | FAIL |
| DIP1 | B | 150 | 13 | -14.7 bp | -24.7 bp | -5.2 bp | -0.75 | FAIL |
| DIP2 | A | 94 | 13 | -20.2 bp | -30.2 bp | -83.5 bp | -0.75 | UNDERPOWERED |
| DIP2 | B | 126 | 13 | -26.3 bp | -36.3 bp | -31.5 bp | -1.34 | FAIL |
| HELD1 | A | 101 | 13 | -52.0 bp | -62.0 bp | -43.9 bp | -1.51 | FAIL |
| HELD1 | B | 202 | 13 | +3.6 bp | -6.4 bp | +13.9 bp | +0.69 | FAIL |
| HELD2 | A | 85 | 13 | -74.9 bp | -84.9 bp | -81.4 bp | -2.75 | UNDERPOWERED |
| HELD2 | B | 174 | 13 | -2.0 bp | -12.0 bp | +2.4 bp | +0.63 | FAIL |
| PRE | A | 294 | 14 | -12.0 bp | -22.0 bp | +15.2 bp | +0.67 | FAIL |
| PRE | B | 472 | 13 | -4.5 bp | -14.5 bp | +0.1 bp | +0.04 | FAIL |
| SQ | A | 419 | 14 | -17.2 bp | -27.2 bp | +12.7 bp | +0.52 | FAIL |
| SQ | B | 523 | 13 | -7.5 bp | -17.5 bp | -4.8 bp | -0.93 | FAIL |

**Verdicts:** CONF1 FAIL, CONF2 UNDERPOWERED, DIP1 INFO, DIP2 INFO, HELD1 INFO, HELD2 INFO, PRE INFO, SQ INFO

**PROMISING tier** (pooled day-equal lift > 0 with t >= 1.5, lift > 0 in each half, pooled net at 3 bp > 0, >= 100 signals and 6 days per half):

- CONF1: pooled lift -27.7 bp (t -1.60, n 289, 26 days); net@3bp pooled -10.7 bp, A -9.5 bp, B -11.9 bp -> not promising
- CONF2: pooled lift -6.6 bp (t -0.27, n 179, 26 days); net@3bp pooled -20.2 bp, A -33.8 bp, B -6.6 bp -> not promising
- DIP1: pooled lift -2.7 bp (t -0.13, n 254, 26 days); net@3bp pooled -29.2 bp, A -40.6 bp, B -17.7 bp -> not promising
- DIP2: pooled lift -57.5 bp (t -1.03, n 220, 26 days); net@3bp pooled -26.3 bp, A -23.2 bp, B -29.3 bp -> not promising
- HELD1: pooled lift -15.0 bp (t -0.82, n 303, 26 days); net@3bp pooled -27.2 bp, A -55.0 bp, B +0.6 bp -> not promising
- HELD2: pooled lift -39.5 bp (t -2.35, n 259, 26 days); net@3bp pooled -41.5 bp, A -77.9 bp, B -5.0 bp -> not promising
- PRE: pooled lift +7.9 bp (t +0.68, n 766, 27 days); net@3bp pooled -11.4 bp, A -15.0 bp, B -7.5 bp -> not promising
- SQ: pooled lift +4.3 bp (t +0.34, n 942, 27 days); net@3bp pooled -15.5 bp, A -20.2 bp, B -10.5 bp -> not promising
- CONF1 minus PRE (pooled lift difference, the value of waiting on the live arm): -35.6 bp; CONF1 minus HELD1 (confirmation of PRE beyond a universe 1-minute effect): -12.6 bp
- CONF2 minus PRE (pooled lift difference, the value of waiting on the live arm): -14.5 bp; CONF2 minus HELD2 (confirmation of PRE beyond a universe 2-minute effect): +33.0 bp
- CONF k=1: decided 545, held 53%, ties 9%, dropped missing/no-print 146, off board at decision 84
- CONF k=2: decided 399, held 45%, ties 7%, dropped missing/no-print 159, off board at decision 217
- HELD k=1: decided 561, held 54%, ties 8%, dropped missing/no-print 164, off board at decision 50
- HELD k=2: decided 503, held 52%, ties 5%, dropped missing/no-print 175, off board at decision 97

### Information only

Lift at other holds (day-equal, t):
- CONF1: 5m -26.6 bp (t -1.88, n 287); 15m -40.4 bp (t -1.49, n 288); 30m -41.9 bp (t -1.32, n 288)
- CONF2: 5m -1.1 bp (t -0.07, n 176); 15m -18.6 bp (t -1.01, n 178); 30m -25.9 bp (t -0.92, n 177)
- DIP1: 5m +29.6 bp (t +1.33, n 253); 15m +27.1 bp (t +1.09, n 254); 30m +15.8 bp (t +0.56, n 251)
- DIP2: 5m -4.5 bp (t -0.26, n 217); 15m -0.7 bp (t -0.02, n 219); 30m -17.3 bp (t -0.54, n 215)
- HELD1: 5m -7.0 bp (t -0.50, n 300); 15m -11.2 bp (t -0.61, n 302); 30m -21.0 bp (t -1.18, n 299)
- HELD2: 5m -10.2 bp (t -0.95, n 259); 15m -14.8 bp (t -0.63, n 259); 30m -23.9 bp (t -0.89, n 256)
- PRE: 5m +15.1 bp (t +1.47, n 762); 15m +12.3 bp (t +0.86, n 762); 30m +12.0 bp (t +0.69, n 761)
- SQ: 5m +4.0 bp (t +0.48, n 935); 15m -8.2 bp (t -0.78, n 935); 30m -8.6 bp (t -0.33, n 928)

Lead before desk fills (latest fire within 15 min):
- CONF1: 126/1118 fills had a prior fire; median lead 2 min; lead >= 3 min 44%; 290 fires, 34% followed by a desk fill within 10 min
- CONF2: 53/1118 fills had a prior fire; median lead 2 min; lead >= 3 min 47%; 179 fires, 23% followed by a desk fill within 10 min
- DIP1: 90/1118 fills had a prior fire; median lead 4 min; lead >= 3 min 59%; 255 fires, 24% followed by a desk fill within 10 min
- DIP2: 49/1118 fills had a prior fire; median lead 6 min; lead >= 3 min 73%; 220 fires, 15% followed by a desk fill within 10 min
- HELD1: 74/1118 fills had a prior fire; median lead 8 min; lead >= 3 min 82%; 304 fires, 14% followed by a desk fill within 10 min
- HELD2: 69/1118 fills had a prior fire; median lead 7 min; lead >= 3 min 87%; 260 fires, 15% followed by a desk fill within 10 min
- PRE: 386/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 44%; 775 fires, 36% followed by a desk fill within 10 min
- SQ: 272/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 45%; 967 fires, 21% followed by a desk fill within 10 min

Other controls and costs (lift unless noted; day-equal, t, n):
- CONF1: cross -26.2 bp (t -1.53, n 287); close_entry -7.4 bp (t -0.61, n 289); net20 -27.7 bp (t -1.53, n 289); later-control overlaps the hold 18%
- CONF2: cross -48.3 bp (t -1.49, n 178); close_entry -6.7 bp (t -0.42, n 179); net20 -37.2 bp (t -2.33, n 179); later-control overlaps the hold 17%
- DIP1: cross -19.1 bp (t -1.57, n 253); close_entry -31.8 bp (t -1.69, n 254); net20 -46.2 bp (t -3.31, n 254); later-control overlaps the hold 19%
- DIP2: cross -10.6 bp (t -0.37, n 219); close_entry -49.1 bp (t -1.11, n 220); net20 -43.3 bp (t -1.62, n 220); later-control overlaps the hold 14%
- HELD1: cross -8.4 bp (t -0.39, n 302); close_entry -11.4 bp (t -0.58, n 303); net20 -44.2 bp (t -2.61, n 303); later-control overlaps the hold 19%
- HELD2: cross -53.5 bp (t -1.33, n 258); close_entry -34.7 bp (t -1.96, n 259); net20 -58.5 bp (t -4.15, n 259); later-control overlaps the hold 17%
- PRE: cross -17.8 bp (t -1.73, n 761); close_entry +7.2 bp (t +0.72, n 766); net20 -28.4 bp (t -3.67, n 766); later-control overlaps the hold 15%
- SQ: cross -16.9 bp (t -1.11, n 938); close_entry +8.9 bp (t +0.74, n 942); net20 -32.5 bp (t -3.85, n 942); later-control overlaps the hold 16%
- every board minute, gross 10m: -7.8 bp (t -2.70, n 75645)

Artifact demo (shift-5 lift on rule-free rising board minutes): +96.0 bp (t +9.31, n 1355)

Drops: {'PRE_off_board': 4407, 'SQ_off_board': 8427, 'CONF1_off_board': 0, 'DIP1_off_board': 0, 'HELD1_off_board': 0, 'CONF2_off_board': 0, 'DIP2_off_board': 0, 'HELD2_off_board': 0, 'no_bars': 107, 'SQ_no_bar': 25, 'PRE_no_bar': 9, 'HELD2_no_bar': 1, 'CONF1_no_bar': 1, 'HELD1_no_bar': 1, 'DIP1_no_bar': 1}
```
