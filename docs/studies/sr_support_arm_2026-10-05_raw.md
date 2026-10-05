# Support-touch arm vs square arm, with exit families (preregs 3255be0 + afc50c0)

seated name-days 554 on 16 days (2026-09-11..2026-10-02); 10/05 not cached (excluded); chrono A 2026-09-11..2026-09-22, B 2026-09-23..2026-10-02
support arms 1165 on 438 name-days; square arms 1809; matched name-days 438; drops {'room_below_min': 559, 'inside_resistance': 345, 'no_resistance_above': 1029, 'refractory_15min': 97}; short series 0

Gross/net in bp per trade. Matched name-days only for support vs square. RT = measured round trip (20 bp).

| exit | half | support n | support gross | support net (t) | square n | square gross | square net | support − square gross (t) | support − same-hour random (t) | other |
|---|---|---|---|---|---|---|---|---|---|---|
| f15 | A | 342 | +1.3 | -18.7 (t -1.08) | 430 | +11.5 | -8.5 | **-10.2** (t -0.40) | +6.4 (t +0.39, n 342) | all-support +1.3 (n 342); square's random ctrl +38.7 |
| f15 | B | 823 | +16.8 | -3.2 (t -0.95) | 913 | +11.7 | -8.3 | **+5.0** (t +0.80) | +13.0 (t +2.35, n 823) | all-support +16.8 (n 823); square's random ctrl +31.7 |
| f15 | all | 1165 | +12.2 | -7.8 (t -1.42) | 1343 | +11.7 | -8.3 | **+0.6** (t +0.07) | +11.1 (t +1.85, n 1165) | all-support +12.2 (n 1165); square's random ctrl +34.2 |
| a | A | 342 | +80.9 | +60.9 (t +0.59) | 430 | +19.0 | -1.0 | **+61.9** (t +1.44) | +29.4 (t +3.69, n 342) | all-support +80.9 (n 342); square's random ctrl +97.8 |
| a | B | 823 | +61.4 | +41.4 (t +1.35) | 913 | +15.3 | -4.7 | **+46.1** (t +1.79) | +20.7 (t +3.25, n 823) | all-support +61.4 (n 823); square's random ctrl +60.3 |
| a | all | 1165 | +67.1 | +47.1 (t +1.30) | 1343 | +16.5 | -3.5 | **+50.6** (t +2.43) | +23.3 (t +4.68, n 1165) | all-support +67.1 (n 1165); square's random ctrl +73.8 |
| b | A | 342 | -8.3 | -28.3 (t -0.55) | 430 | +14.3 | -5.7 | **-22.5** (t -0.41) | +0.6 (t +0.02, n 342) | all-support -8.3 (n 342); square's random ctrl +84.0 |
| b | B | 823 | +29.4 | +9.4 (t +0.53) | 913 | +6.2 | -13.8 | **+23.2** (t +0.93) | +17.9 (t +2.66, n 823) | all-support +29.4 (n 823); square's random ctrl +36.6 |
| b | all | 1165 | +18.4 | -1.6 (t -0.09) | 1343 | +8.8 | -11.2 | **+9.6** (t +0.39) | +12.8 (t +1.24, n 1165) | all-support +18.4 (n 1165); square's random ctrl +53.7 |
| c | A | 342 | +14.1 | -5.9 (t -1.03) | 430 | +11.0 | -9.0 | **+3.1** (t +0.08) | -3.6 (t -0.31, n 342) | all-support +14.1 (n 342); square's random ctrl +98.4 |
| c | B | 823 | +12.4 | -7.6 (t -1.14) | 913 | -3.6 | -23.6 | **+16.1** (t +0.80) | +14.7 (t +4.87, n 823) | all-support +12.4 (n 823); square's random ctrl +27.6 |
| c | all | 1165 | +12.9 | -7.1 (t -1.46) | 1343 | +1.1 | -18.9 | **+11.9** (t +0.64) | +9.4 (t +2.19, n 1165) | all-support +12.9 (n 1165); square's random ctrl +53.0 |
| d60 | A | 342 | +50.9 | +30.9 (t +0.52) | 430 | +26.6 | +6.6 | **+24.3** (t +0.63) | +70.7 (t +1.98, n 342) | all-support +50.9 (n 342); square's random ctrl +85.5 |
| d60 | B | 823 | +34.2 | +14.2 (t +1.56) | 913 | +6.0 | -14.0 | **+28.2** (t +2.34) | +18.0 (t +2.24, n 823) | all-support +34.2 (n 823); square's random ctrl +52.2 |
| d60 | all | 1165 | +39.1 | +19.1 (t +1.05) | 1343 | +12.6 | -7.4 | **+26.5** (t +2.06) | +33.5 (t +2.56, n 1165) | all-support +39.1 (n 1165); square's random ctrl +64.2 |
| d120 | A | 342 | +48.9 | +28.9 (t +0.35) | 430 | +22.8 | +2.8 | **+26.1** (t +0.44) | +23.2 (t +2.52, n 342) | all-support +48.9 (n 342); square's random ctrl +97.1 |
| d120 | B | 823 | +52.3 | +32.3 (t +1.87) | 913 | +12.5 | -7.5 | **+39.8** (t +1.72) | +30.8 (t +3.11, n 823) | all-support +52.3 (n 823); square's random ctrl +55.4 |
| d120 | all | 1165 | +51.3 | +31.3 (t +1.20) | 1343 | +15.8 | -4.2 | **+35.5** (t +1.60) | +28.5 (t +3.91, n 1165) | all-support +51.3 (n 1165); square's random ctrl +70.4 |
| d240 | A | 342 | +76.0 | +56.0 (t +0.56) | 430 | +32.7 | +12.7 | **+43.3** (t +0.85) | +28.4 (t +2.73, n 342) | all-support +76.0 (n 342); square's random ctrl +108.2 |
| d240 | B | 823 | +60.9 | +40.9 (t +1.63) | 913 | +18.7 | -1.3 | **+42.2** (t +1.95) | +17.6 (t +2.43, n 823) | all-support +60.9 (n 823); square's random ctrl +61.7 |
| d240 | all | 1165 | +65.3 | +45.3 (t +1.36) | 1343 | +23.2 | +3.2 | **+42.1** (t +2.11) | +20.7 (t +3.57, n 1165) | all-support +65.3 (n 1165); square's random ctrl +78.4 |

## Info: support-arm variants (all support arms in the variant vs all square arms; exits f15 and b)
- room>=0.25%, exit f15: A: n 392 gross +2.5 net -17.5 | vs square -8.9 (t -0.42) || B: n 937 gross +15.0 net -5.0 | vs square +4.4 (t +0.79)
- room>=0.25%, exit b: A: n 392 gross -5.9 net -25.9 | vs square -51.1 (t -1.71) || B: n 937 gross +28.5 net +8.5 | vs square +18.7 (t +1.03)
- room>=0.60%, exit f15: A: n 283 gross -0.9 net -20.9 | vs square -12.3 (t -0.47) || B: n 671 gross +16.0 net -4.0 | vs square +5.4 (t +1.01)
- room>=0.60%, exit b: A: n 283 gross -8.9 net -28.9 | vs square -54.0 (t -1.44) || B: n 671 gross +31.4 net +11.4 | vs square +21.7 (t +1.10)
- room>=0.40% + %R<=-50, exit f15: A: n 191 gross -20.5 net -40.5 | vs square -31.9 (t -1.05) || B: n 470 gross +14.8 net -5.2 | vs square +4.2 (t +0.86)
- room>=0.40% + %R<=-50, exit b: A: n 191 gross +6.6 net -13.4 | vs square -38.6 (t -1.20) || B: n 470 gross +29.4 net +9.4 | vs square +19.6 (t +1.24)

## Minutes earlier (support arm vs square arm)
- matched name-days 438: first square − first support = median +32 min, mean +24 min; support first on 59%, square first on 40%, same minute 1%
- per square arm: 957/1809 (53%) had an earlier support arm the same name-day; minutes since it: median 69, IQR 29–128

## Info: earlier S/R ideas rerun with exits (a) 15:55 and (b) resistance touch (own samples, chronological 10/10 halves)
| idea | exit | half | n | gross | net (t) | control gross | signal − control (t) |
|---|---|---|---|---|---|---|---|
| gap arm (−60 %R near support) | a | A | 355 | +44.3 | +24.3 (t +0.29) | +25.0 | +19.3 (t +9.67) |
| gap arm (−60 %R near support) | a | B | 1800 | +38.0 | +18.0 (t +0.79) | +32.5 | +5.5 (t +2.54) |
| gap arm (−60 %R near support) | b | A | 355 | +11.7 | -8.3 (t -0.25) | -11.8 | +23.5 (t +0.79) |
| gap arm (−60 %R near support) | b | B | 1800 | +14.9 | -5.1 (t -0.44) | +7.9 | +7.0 (t +1.71) |
| range trade S→R | a | A | 77 | -39.4 | -59.4 (t -0.31) | -18.0 | -21.4 (t -1.50) |
| range trade S→R | a | B | 408 | +118.1 | +98.1 (t +1.58) | +106.4 | +11.7 (t +1.55) |
| range trade S→R | b | A | 77 | -59.1 | -79.1 (t -0.60) | -13.1 | -46.0 (t -1.22) |
| range trade S→R | b | B | 408 | +24.7 | +4.7 (t +0.26) | +16.0 | +8.7 (t +0.85) |

**PARENT PRIMARY (3255be0, 15-min hold): FAIL** (bar: support − square gross on matched name-days > measured RT 20 bp in BOTH chronological halves, >= 50 support arms per half)
- half A: n support 342, support − square -10.2 bp (t -0.40) vs RT 20.0; net support -18.7 / square -8.5 bp
- half B: n support 823, support − square +5.0 bp (t +0.80) vs RT 20.0; net support -3.2 / square -8.3 bp
**ADDENDUM PRIMARY (afc50c0, exit b): FAIL** (bar: support − square gross on matched name-days > measured RT 20 bp in BOTH chronological halves, >= 50 support arms per half)
- half A: n support 342, support − square -22.5 bp (t -0.41) vs RT 20.0; net support -28.3 / square -5.7 bp
- half B: n support 823, support − square +23.2 bp (t +0.93) vs RT 20.0; net support +9.4 / square -13.8 bp
