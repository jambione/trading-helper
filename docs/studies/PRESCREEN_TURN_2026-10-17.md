# Pre-screen turn result 2026-10-17: BOTHUP FAIL, MID FAIL (both halves, both gates)

Prereg docs/studies/prescreen_turn_prereg.json + amended_1 (skeptic round 2 READY). Warehouse 119 sessions 2026-04-28..2026-10-15, 400 seeded unscreened liquid names/session, IEX %R lines warmed on the prior session, SIP markouts, later-only same-name control, 10 bp.

On the unscreened universe 1-minute %R turns are exactly a random minute: BOTHUP pooled lift -0.2 bp (t -1.17, n 283,456), MID -0.0 bp (t -0.10, n 227,978); OSL/PRE/SQ about 0; every sampled minute -0.0 bp (n 12.1M). Both fail the cross-name control. Net at 3 bp about -3 bp = cost. BOTHUP fires a median 9 min before desk fills (it does see the turn early), but the turn carries no information. The look-ahead shift demo reads +32.6 bp (t 107), the AGY mirage, so the harness is honest. if_fail: no edge on the unscreened universe either; early seating (#16) skipped; strong evidence for the 10/30 verdict on the intraday %R design.

```
| BOTHUP | A | 145849 | 60 | -0.5 bp | -10.5 bp | -0.3 bp | -1.07 | FAIL |
| BOTHUP | B | 137607 | 59 | +0.1 bp | -9.9 bp | -0.1 bp | -0.51 | FAIL |
| MID | A | 116771 | 60 | -0.4 bp | -10.4 bp | -0.3 bp | -1.18 | FAIL |
| MID | B | 111207 | 59 | +0.1 bp | -9.9 bp | +0.3 bp | +1.20 | FAIL |
| OSL | A | 59089 | 60 | -0.0 bp | -10.0 bp | -0.2 bp | -0.44 | FAIL |
| OSL | B | 56010 | 59 | +0.5 bp | -9.5 bp | -0.1 bp | -0.24 | FAIL |
| PRE | A | 44928 | 60 | -0.1 bp | -10.1 bp | +0.2 bp | +0.52 | FAIL |
| PRE | B | 43347 | 59 | -0.4 bp | -10.4 bp | -0.0 bp | -0.11 | FAIL |
| SQ | A | 66896 | 60 | -0.2 bp | -10.2 bp | +0.1 bp | +0.22 | FAIL |
| SQ | B | 63568 | 59 | -0.4 bp | -10.4 bp | -0.2 bp | -0.73 | FAIL |
**Verdicts:** BOTHUP FAIL, MID FAIL, OSL INFO, PRE INFO, SQ INFO
- BOTHUP: pooled lift -0.2 bp (t -1.17, n 283456, 119 days); net@3bp pooled -3.2 bp, A -3.5 bp, B -2.9 bp -> not promising
- MID: pooled lift -0.0 bp (t -0.10, n 227978, 119 days); net@3bp pooled -3.2 bp, A -3.4 bp, B -2.9 bp -> not promising
- OSL: pooled lift -0.1 bp (t -0.50, n 115099, 119 days); net@3bp pooled -2.8 bp, A -3.0 bp, B -2.5 bp -> not promising
- PRE: pooled lift +0.1 bp (t +0.36, n 88275, 119 days); net@3bp pooled -3.2 bp, A -3.1 bp, B -3.4 bp -> not promising
- SQ: pooled lift -0.1 bp (t -0.30, n 130464, 119 days); net@3bp pooled -3.3 bp, A -3.2 bp, B -3.4 bp -> not promising
OSL warm-up coverage: days excluded (> 5% of board names lacking prior bars) []
- every board minute, gross 10m: -0.0 bp (t -0.17, n 12135488)
Artifact demo (shift-5 lift on rule-free rising board minutes): +32.6 bp (t +106.88, n 29405)
Drops: {'BOTHUP_off_board': 0, 'MID_off_board': 0, 'OSL_off_board': 0, 'PRE_off_board': 0, 'SQ_off_board': 0, 'OSL_no_bar': 22869, 'BOTHUP_no_bar': 54912, 'MID_no_bar': 44187, 'SQ_no_bar': 25042, 'PRE_no_bar': 15411, 'no_bars': 2115, 'BOTHUP_no_control': 3, 'SQ_no_control': 1, 'OSL_no_control': 1, 'MID_no_control': 2}
- BOTHUP: cross-name lift A -0.6 bp, B +0.0 bp -> FAILS the cross control
- MID: cross-name lift A -0.2 bp, B -0.1 bp -> FAILS the cross control
- days excluded for missing warm-up: 0 (cap 6)
- BOTHUP: pooled lift -0.2 bp (t -1.17, n 283456, 119 days); net@3bp pooled -3.2 bp, A -3.5 bp, B -2.9 bp -> not promising
- MID: pooled lift -0.0 bp (t -0.10, n 227978, 119 days); net@3bp pooled -3.2 bp, A -3.4 bp, B -2.9 bp -> not promising
- BOTHUP: 5m -0.1 bp (t -1.06, n 266190); 15m -0.3 bp (t -1.40, n 266522); 30m -0.1 bp (t -0.16, n 265031)
- MID: 5m -0.2 bp (t -1.97, n 213887); 15m +0.0 bp (t +0.02, n 214069); 30m +0.3 bp (t +1.01, n 212916)
- BOTHUP: 89/1721 fills had a prior fire; median lead 9 min; lead >= 3 min 90%; 338371 fires, 0% followed by a desk fill within 10 min
- MID: 98/1721 fills had a prior fire; median lead 5 min; lead >= 3 min 76%; 272167 fires, 0% followed by a desk fill within 10 min
```
