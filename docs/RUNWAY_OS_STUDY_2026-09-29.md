# Runway / oversold study 2026-09-29

Bars: 1493 symbol-days in the runway cache. Synthetic signals: 3598 square, 3126 leave-oversold, 6724 combined. Recorded fills with bars: 975. Sample split at 2026-08-31. Cost 0.20% round trip. 1R on a synthetic entry is 5% of price.

1-minute bars understate the live chase (seven idle steps stand in for a quiet minute). Two in-zone bars stand in for two arm polls, which is stricter than the live debounce, so short-lived triangles are missing.

| set | exit | n | win% | mean R | med MFE | net $ | $ before | n | $ after | n |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| square | live | 3598 | 24.4 | -0.045 | 0.040 | -620 | -27 | 496 | -593 | 3102 |
| square | g12_eod | 3598 | 34.7 | -0.041 | 0.070 | -513 | -26 | 496 | -487 | 3102 |
| square | g10_eod | 3598 | 34.7 | -0.041 | 0.070 | -513 | -26 | 496 | -487 | 3102 |
| square | g12_red | 3598 | 26.2 | -0.042 | 0.050 | -564 | -22 | 496 | -543 | 3102 |
| square | g12_red_d30 | 3598 | 26.2 | -0.042 | 0.050 | -565 | -22 | 496 | -543 | 3102 |
| square | g12_red_d40 | 3598 | 26.2 | -0.042 | 0.050 | -564 | -22 | 496 | -542 | 3102 |
| square | g12_red_d30_1bar | 3598 | 26.2 | -0.042 | 0.050 | -564 | -22 | 496 | -543 | 3102 |
| square | g12_peakoff | 3598 | 25.5 | -0.043 | 0.050 | -566 | -23 | 496 | -543 | 3102 |
| oversold | live | 3126 | 38.3 | -0.036 | 0.074 | -247 | -15 | 394 | -232 | 2732 |
| oversold | g12_eod | 3126 | 35.3 | -0.038 | 0.073 | -219 | -8 | 394 | -211 | 2732 |
| oversold | g10_eod | 3126 | 35.3 | -0.038 | 0.073 | -219 | -8 | 394 | -211 | 2732 |
| oversold | g12_red | 3126 | 26.6 | -0.044 | 0.051 | -284 | -18 | 394 | -266 | 2732 |
| oversold | g12_red_d30 | 3126 | 26.6 | -0.044 | 0.051 | -284 | -18 | 394 | -266 | 2732 |
| oversold | g12_red_d40 | 3126 | 26.6 | -0.044 | 0.051 | -284 | -18 | 394 | -266 | 2732 |
| oversold | g12_red_d30_1bar | 3126 | 26.6 | -0.044 | 0.051 | -284 | -18 | 394 | -266 | 2732 |
| oversold | g12_peakoff | 3126 | 26.0 | -0.044 | 0.051 | -293 | -18 | 394 | -275 | 2732 |
| both | live | 6724 | 30.9 | -0.041 | 0.060 | -867 | -42 | 890 | -825 | 5834 |
| both | g12_eod | 6724 | 35.0 | -0.040 | 0.071 | -732 | -34 | 890 | -698 | 5834 |
| both | g10_eod | 6724 | 35.0 | -0.040 | 0.071 | -732 | -34 | 890 | -698 | 5834 |
| both | g12_red | 6724 | 26.4 | -0.043 | 0.051 | -848 | -40 | 890 | -809 | 5834 |
| both | g12_red_d30 | 6724 | 26.4 | -0.043 | 0.051 | -849 | -39 | 890 | -810 | 5834 |
| both | g12_red_d40 | 6724 | 26.4 | -0.043 | 0.051 | -848 | -40 | 890 | -809 | 5834 |
| both | g12_red_d30_1bar | 6724 | 26.4 | -0.043 | 0.051 | -848 | -40 | 890 | -809 | 5834 |
| both | g12_peakoff | 6724 | 25.7 | -0.044 | 0.051 | -859 | -41 | 890 | -818 | 5834 |
| fills | live | 975 | 36.2 | -0.048 | 0.084 | -729 | -507 | 537 | -222 | 438 |
| fills | g12_eod | 975 | 40.9 | -0.044 | 0.099 | -445 | -250 | 537 | -195 | 438 |
| fills | g10_eod | 975 | 40.9 | -0.044 | 0.099 | -445 | -250 | 537 | -195 | 438 |
| fills | g12_red | 975 | 30.9 | -0.046 | 0.075 | -632 | -417 | 537 | -215 | 438 |
| fills | g12_red_d30 | 975 | 30.9 | -0.046 | 0.075 | -632 | -417 | 537 | -215 | 438 |
| fills | g12_red_d40 | 975 | 30.9 | -0.046 | 0.075 | -632 | -417 | 537 | -215 | 438 |
| fills | g12_red_d30_1bar | 975 | 30.9 | -0.046 | 0.075 | -632 | -417 | 537 | -215 | 438 |
| fills | g12_peakoff | 975 | 30.7 | -0.045 | 0.076 | -692 | -444 | 537 | -249 | 438 |

## Which leash binds

Counted on armed bars (MFE ≥ 0.06R). `peak` means the 0.35% under the high is tighter than give_r. `clip` means give_r × R was wider than the 1% price cap.

| set | exit | give binds | peak binds | tie | 1% cap clips |
|---|---|---:|---:|---:|---:|
| both | live | 0 | 9968 | 59 | 10027 |
| both | g12_eod | 0 | 11885 | 137 | 0 |
| both | g10_eod | 0 | 11823 | 199 | 0 |
| both | g12_red | 0 | 8917 | 137 | 0 |
| both | g12_red_d30 | 0 | 8913 | 137 | 0 |
| both | g12_red_d40 | 0 | 8917 | 137 | 0 |
| both | g12_red_d30_1bar | 0 | 8917 | 137 | 0 |
| both | g12_peakoff | 10327 | 0 | 0 | 0 |
| fills | live | 0 | 1423 | 0 | 1355 |
| fills | g12_eod | 28 | 1611 | 7 | 0 |
| fills | g10_eod | 61 | 1575 | 7 | 0 |
| fills | g12_red | 11 | 1145 | 1 | 0 |
| fills | g12_red_d30 | 11 | 1145 | 1 | 0 |
| fills | g12_red_d40 | 11 | 1145 | 1 | 0 |
| fills | g12_red_d30_1bar | 11 | 1145 | 1 | 0 |
| fills | g12_peakoff | 1572 | 0 | 0 | 0 |

## Exit reasons (both-arms signals)

- live: dead 878, eod 296, left_overbought 2361, stop 3189
- g12_eod: eod 1021, stop 5703
- g10_eod: eod 1021, stop 5703
- g12_red: eod 227, stop 6497
- g12_red_d30: eod 227, rsi_dump 33, stop 6464
- g12_red_d40: eod 227, rsi_dump 1, stop 6496
- g12_red_d30_1bar: eod 227, stop 6497
- g12_peakoff: eod 233, stop 6491

## Knobs written after this table

Judged on stop+EOD (`g12_eod`), not on the red-chase overlay. Leave-oversold mean R −0.038 beat square −0.041. Both arms −0.040 was worse than oversold alone, and both-arms dollars (−732) add the square cohort (−513) on top of oversold (−219). `bot_config`: oversold arm on, square arm off, give_r 0.12, peak give left at 0.35%, leave-OB off, dead-trade min 0, red step 0.02, RSI dump off.

Give 0.12 and give 0.10 matched while the 0.35% peak leash was on (give binds 0 on the both-arms armed bars). Peak-off was worse (−859 vs −732). The 30-point dump fired 33 times on the combined set and did not beat stop+EOD on either half (−849 vs −732). The 40-point dump fired once. The one-bar proxy fired zero times.

