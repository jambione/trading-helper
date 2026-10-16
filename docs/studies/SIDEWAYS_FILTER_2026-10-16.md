# Sideways/downtrend filter result 2026-10-16: SLOWUP FAIL, TRER UNDERPOWERED; not promising

Prereg docs/studies/sideways_filter_prereg.json + amended_1 (skeptic READY 982355a, before scoring). 28 sessions, warm lines.

Operator's %R read (SLOWUP: slow line +5 pts / 15 min) points the right way pooled - kept +11.7 bp lift vs skipped (flat/falling) -18.4 bp, unfiltered same signals +9.2, random minutes passing the read -21.3 - but t 1.0, the halves disagree (A kept +24.9 vs skipped -41.8; B kept -2.6 vs skipped +5.1, reversed), the skipped set is underpowered in half A, and the kept arms still net -6.8 bp at the measured 3 bp cost. The price read (TRER) kept fewer, worse signals than it skipped. Closed per if_fail; the direction is recorded on the running list.

```
| rule | half | n | days | gross | net | lift | t | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| TRER | A | 73 | 14 | -22.0 bp | -32.0 bp | +12.7 bp | +0.19 | UNDERPOWERED |
| TRER | B | 66 | 13 | -9.7 bp | -19.7 bp | -7.4 bp | -0.63 | UNDERPOWERED |
| SLOWUP | A | 201 | 14 | -3.6 bp | -13.6 bp | +24.9 bp | +1.13 | FAIL |
| SLOWUP | B | 322 | 13 | -4.0 bp | -14.0 bp | -2.6 bp | -0.71 | FAIL |
| CHOP | A | 200 | 13 | -3.0 bp | -13.0 bp | +25.2 bp | +1.25 | FAIL |
| CHOP | B | 384 | 13 | -7.0 bp | -17.0 bp | -2.0 bp | -0.57 | FAIL |
| SLOWFLAT | A | 87 | 13 | -48.0 bp | -58.0 bp | -41.8 bp | -1.24 | UNDERPOWERED |
| SLOWFLAT | B | 149 | 13 | -3.9 bp | -13.9 bp | +5.1 bp | +0.64 | FAIL |
| PREER | A | 273 | 14 | -10.3 bp | -20.3 bp | +17.5 bp | +0.94 | FAIL |
| PREER | B | 450 | 13 | -6.9 bp | -16.9 bp | -2.5 bp | -0.74 | FAIL |
| PRESLOW | A | 288 | 14 | -10.6 bp | -20.6 bp | +17.5 bp | +0.95 | FAIL |
| PRESLOW | B | 471 | 13 | -4.8 bp | -14.8 bp | +0.2 bp | +0.05 | FAIL |
| RNDTRER | A | 46 | 11 | -45.7 bp | -55.7 bp | -102.1 bp | -1.20 | UNDERPOWERED |
| RNDTRER | B | 48 | 13 | +8.2 bp | -1.8 bp | +6.3 bp | +0.53 | UNDERPOWERED |
| RNDSLOWUP | A | 103 | 13 | -35.2 bp | -45.2 bp | -42.3 bp | -1.36 | FAIL |
| RNDSLOWUP | B | 157 | 13 | +3.8 bp | -6.2 bp | -0.4 bp | -0.06 | FAIL |
| PRE | A | 294 | 14 | -12.0 bp | -22.0 bp | +15.2 bp | +0.67 | FAIL |
| PRE | B | 472 | 13 | -4.5 bp | -14.5 bp | +0.1 bp | +0.04 | FAIL |
| SQ | A | 419 | 14 | -17.2 bp | -27.2 bp | +12.7 bp | +0.52 | FAIL |
| SQ | B | 523 | 13 | -7.5 bp | -17.5 bp | -4.8 bp | -0.93 | FAIL |
**Verdicts:** TRER UNDERPOWERED, SLOWUP FAIL, CHOP INFO, SLOWFLAT INFO, PREER INFO, PRESLOW INFO, RNDTRER INFO, RNDSLOWUP INFO, PRE INFO, SQ INFO
- TRER: pooled lift +3.0 bp (t +0.08, n 139, 27 days); net@3bp pooled -19.1 bp, A -25.0 bp, B -12.7 bp -> not promising
- SLOWUP: pooled lift +11.7 bp (t +1.00, n 523, 27 days); net@3bp pooled -6.8 bp, A -6.6 bp, B -7.0 bp -> not promising
- CHOP: pooled lift +11.6 bp (t +1.12, n 584, 26 days); net@3bp pooled -8.0 bp, A -6.0 bp, B -10.0 bp -> not promising
- SLOWFLAT: pooled lift -18.4 bp (t -1.04, n 236, 26 days); net@3bp pooled -28.9 bp, A -51.0 bp, B -6.9 bp -> not promising
- PREER: pooled lift +7.9 bp (t +0.80, n 723, 27 days); net@3bp pooled -11.7 bp, A -13.3 bp, B -9.9 bp -> not promising
- PRESLOW: pooled lift +9.2 bp (t +0.95, n 759, 27 days); net@3bp pooled -10.8 bp, A -13.6 bp, B -7.8 bp -> not promising
- RNDTRER: pooled lift -43.4 bp (t -1.08, n 94, 24 days); net@3bp pooled -19.5 bp, A -48.7 bp, B +5.2 bp -> not promising
- RNDSLOWUP: pooled lift -21.3 bp (t -1.33, n 260, 26 days); net@3bp pooled -18.7 bp, A -38.2 bp, B +0.8 bp -> not promising
- PRE: pooled lift +7.9 bp (t +0.68, n 766, 27 days); net@3bp pooled -11.4 bp, A -15.0 bp, B -7.5 bp -> not promising
- SQ: pooled lift +4.3 bp (t +0.34, n 942, 27 days); net@3bp pooled -15.5 bp, A -20.2 bp, B -10.5 bp -> not promising
- TRER comparisons (pooled lift): kept +3.0 bp, skipped +11.6 bp, same signals unfiltered +7.9 bp, random minutes passing the read -43.4 bp; net@3bp kept -19.1 bp vs skipped -8.0 bp; skipped set powered True -> comparisons FAIL
- SLOWUP comparisons (pooled lift): kept +11.7 bp, skipped -18.4 bp, same signals unfiltered +9.2 bp, random minutes passing the read -21.3 bp; net@3bp kept -6.8 bp vs skipped -28.9 bp; skipped set powered False -> comparisons FAIL
- TRER: 5m -0.8 bp (t -0.03, n 138); 15m +21.6 bp (t +0.51, n 139); 30m +11.3 bp (t +0.69, n 138)
- SLOWUP: 5m +18.5 bp (t +1.68, n 522); 15m +16.0 bp (t +0.93, n 521); 30m +13.3 bp (t +1.05, n 520)
- CHOP: 5m +19.5 bp (t +1.39, n 583); 15m +12.7 bp (t +0.72, n 581); 30m +2.3 bp (t +0.11, n 580)
- SLOWFLAT: 5m -16.9 bp (t -0.56, n 234); 15m +8.6 bp (t +0.52, n 235); 30m -45.5 bp (t -2.10, n 234)
- PREER: 5m +16.3 bp (t +1.63, n 721); 15m +16.3 bp (t +1.06, n 720); 30m +0.4 bp (t +0.03, n 718)
- PRESLOW: 5m +18.0 bp (t +1.70, n 756); 15m +14.9 bp (t +1.03, n 756); 30m +1.5 bp (t +0.13, n 754)
- RNDTRER: 5m -17.1 bp (t -0.88, n 93); 15m -33.4 bp (t -1.26, n 92); 30m -38.2 bp (t -1.05, n 94)
- RNDSLOWUP: 5m -7.2 bp (t -0.50, n 260); 15m -24.9 bp (t -1.12, n 258); 30m -23.4 bp (t -0.62, n 258)
- PRE: 5m +15.1 bp (t +1.47, n 762); 15m +12.3 bp (t +0.86, n 762); 30m +12.0 bp (t +0.69, n 761)
- SQ: 5m +4.0 bp (t +0.48, n 935); 15m -8.2 bp (t -0.78, n 935); 30m -8.6 bp (t -0.33, n 928)
- TRER: 33/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 24%; 141 fires, 21% followed by a desk fill within 10 min
- SLOWUP: 272/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 44%; 527 fires, 37% followed by a desk fill within 10 min
- CHOP: 339/1118 fills had a prior fire; median lead 2 min; lead >= 3 min 45%; 590 fires, 41% followed by a desk fill within 10 min
- SLOWFLAT: 113/1118 fills had a prior fire; median lead 2 min; lead >= 3 min 43%; 240 fires, 36% followed by a desk fill within 10 min
- PREER: 372/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 43%; 731 fires, 37% followed by a desk fill within 10 min
- PRESLOW: 384/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 43%; 767 fires, 37% followed by a desk fill within 10 min
- RNDTRER: 13/1118 fills had a prior fire; median lead 3 min; lead >= 3 min 62%; 95 fires, 9% followed by a desk fill within 10 min
- RNDSLOWUP: 51/1118 fills had a prior fire; median lead 7 min; lead >= 3 min 76%; 262 fires, 13% followed by a desk fill within 10 min
- PRE: 386/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 44%; 775 fires, 36% followed by a desk fill within 10 min
- SQ: 272/1118 fills had a prior fire; median lead 1 min; lead >= 3 min 45%; 967 fires, 21% followed by a desk fill within 10 min
- TRER: cross -3.4 bp (t -0.12, n 138); close_entry +12.3 bp (t +0.40, n 139); net20 -36.1 bp (t -2.68, n 139); later-control overlaps the hold 14%
- SLOWUP: cross -14.2 bp (t -0.80, n 519); close_entry +13.4 bp (t +1.38, n 523); net20 -23.8 bp (t -2.65, n 523); later-control overlaps the hold 14%
- CHOP: cross -12.8 bp (t -0.60, n 580); close_entry +9.5 bp (t +0.81, n 584); net20 -25.0 bp (t -1.93, n 584); later-control overlaps the hold 14%
- SLOWFLAT: cross -23.1 bp (t -1.04, n 235); close_entry -12.4 bp (t -1.21, n 236); net20 -45.9 bp (t -4.05, n 236); later-control overlaps the hold 16%
- PREER: cross -13.3 bp (t -1.00, n 718); close_entry +6.3 bp (t +0.78, n 723); net20 -28.7 bp (t -3.62, n 723); later-control overlaps the hold 14%
```
