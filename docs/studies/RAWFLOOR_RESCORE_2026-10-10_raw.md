# Raw-price re-score, 2026-05-01..2026-09-11 (92 days), raw source days {'sr_cache': 91, 'fetched': 1}
name-days 3581; no raw RTH bars 0; raw 09:30 open < $10: 66
adj->raw factor: share within 1% of 1.0 = 0.980; factor < 0.5 (reverse split later) 65; factor > 2 (forward split later) 0
below-$10 name-days (sym day raw_open factor): BYND 2026-05-01 1.08 x0.0333, EZGO 2026-05-06 2.62 x0.00667, ALIT 2026-05-06 1.03 x0.05, BIYA 2026-05-06 1.48 x0.1, GDC 2026-05-07 1.38 x0.004, GDC 2026-05-08 0.23 x0.004, AIIO 2026-05-12 1.43 x0.1, TDIC 2026-05-13 2.99 x0.04, AEHL 2026-05-13 3.02 x0.0625, AIIO 2026-05-13 1.45 x0.1, AEHL 2026-05-14 5.15 x0.0625, AIIO 2026-05-14 4.38 x0.1, YMAT 2026-05-15 2.24 x0.2, ALP 2026-05-15 0.34 x0.02, TDIC 2026-05-15 1.55 x0.04, HUBC 2026-05-18 0.15 x0.002, AIIO 2026-05-18 4.60 x0.1, QNCX 2026-05-19 1.04 x0.05, HUBC 2026-05-20 0.15 x0.002, JUNS 2026-05-21 0.23 x0.0133, HCWB 2026-05-22 3.21 x0.167, CXAI 2026-05-22 0.22 x0.02, NXXT 2026-05-26 1.02 x0.1, ARTL 2026-05-26 1.91 x0.111, BIYA 2026-05-26 1.65 x0.1, VCIG 2026-05-26 1.66 x0.0667, AEMD 2026-05-27 2.83 x0.2, UZX 2026-05-27 1.05 x0.0435, VCIG 2026-05-27 3.43 x0.0667, HUBC 2026-06-01 0.53 x0.002, VCIG 2026-06-01 9.21 x0.0667, DBGI 2026-06-02 0.96 x0.025, HUBC 2026-06-03 0.72 x0.002, PMI 2026-06-03 0.33 x0.02, HUBC 2026-06-05 0.23 x0.002, CXAI 2026-06-05 0.28 x0.02, SMTK 2026-06-08 0.62 x0.02, GMM 2026-06-09 0.21 x0.02, WOK 2026-06-09 0.11 x0.01, CPOP 2026-06-10 0.51 x0.00667, GMEX 2026-06-10 0.93 x0.0123, HKIT 2026-06-11 0.48 x0.0016, BYAH 2026-06-12 2.87 x0.125, RUBI 2026-06-12 0.70 x0.04, HKIT 2026-06-12 0.58 x0.0016, RUBI 2026-06-15 0.52 x0.04, CIIT 2026-06-15 1.91 x0.1, BYAH 2026-06-15 1.94 x0.125, HUBC 2026-06-16 1.83 x0.04, PAVS 2026-06-16 0.20 x0.01, ALIT 2026-06-16 0.68 x0.05, CDT 2026-06-22 1.42 x0.004, CDT 2026-06-23 1.56 x0.004, INLF 2026-06-29 0.07 x0.005, CTNT 2026-06-30 1.75 x0.00667, LGCL 2026-06-30 0.95 x0.008, JEM 2026-07-01 8.60 x0.0833, JEM 2026-07-02 3.53 x0.0833, ELPW 2026-07-13 0.33 x0.0222, VMAR 2026-07-14 1.81 x0.1, AEHL 2026-07-23 0.73 x0.0625, GOSS 2026-07-28 0.27 x0.0125, LGHL 2026-07-29 1.21 x0.05, TRUG 2026-08-18 1.71 x0.1, EPOW 2026-08-28 0.51 x0.04, NOK 2026-09-04 9.99 x1

## SIP breakout, as published (all events)
  arm                               half         n med spr  gross15       net15 (t)   net30
  ALL signals                       first     3593    20.1     +5.0    -27.9 (-5.25)   -22.8
  ALL signals                       second    3538    13.9     -5.1    -29.3 (-10.14)   -30.8
  CAPPED signals (spr <= 5 bp)      first      323     3.8     +0.9     -2.8 (-0.88)    -2.7
  CAPPED signals (spr <= 5 bp)      second     417     3.4     +0.2     -3.2 (-1.47)    -3.5
  CAPPED + quiet-tape filter        first      322     3.8     +0.9     -2.7 (-0.89)    -2.8
  CAPPED + quiet-tape filter        second     417     3.4     +0.2     -3.2 (-1.47)    -3.5
  CAPPED random control             first      287     3.8     +9.1     +5.5 (+0.83)    +9.0
  CAPPED random control             second     419     3.6     +2.2     -1.3 (+0.90)    -3.4
  VERDICT: FAIL

SIP sig events from raw<$10 name-days: 206 of 7131 (capped 7, name-days 61)
SIP ctl events from raw<$10 name-days: 208 of 7134 (capped 10, name-days 61)
SIP events from name-days with no raw bars (also excluded): 0

## SIP breakout, raw open >= $10 only
  arm                               half         n med spr  gross15       net15 (t)   net30
  ALL signals                       first     3407    19.3     -0.8    -31.7 (-9.51)   -32.2
  ALL signals                       second    3518    13.9     -4.7    -28.8 (-10.20)   -30.0
  CAPPED signals (spr <= 5 bp)      first      317     3.8     -3.0     -6.7 (-1.72)   -11.5
  CAPPED signals (spr <= 5 bp)      second     416     3.4     -2.5     -5.9 (-2.24)    -4.1
  CAPPED + quiet-tape filter        first      316     3.8     -3.0     -6.7 (-1.73)   -11.5
  CAPPED + quiet-tape filter        second     416     3.4     -2.5     -5.9 (-2.24)    -4.1
  CAPPED random control             first      278     3.8     +3.2     -0.5 (-0.97)    +1.5
  CAPPED random control             second     418     3.6     +2.9     -0.6 (+1.09)    -2.4
  VERDICT: FAIL

## SR breakout history: events 17032, from raw<$10 name-days 0 (the script already dropped 66 raw<$10/no-RTH name-days)
SR scored name-days 3127; of those below $10 by this check: 0

## Needle C2: moments 223190
band reconstruction check: adjusted-band mismatches 0 of 223190; px missing 0; no raw factor 0
C2 name-days: adjusted band 134, raw band 134, leave 0 (), enter 0 ()

### C2, ADJUSTED $20-100 band (as published, reproduces -24.9 in the prereg order)
- half A: moments 2037, name-days 55, days 29 | net30 -11.9 (t -2.12) | minus control +2.0 (t +0.48)
- half B: moments 2765, name-days 79, days 34 | net30 -10.3 (t -2.68) | minus control -0.8 (t -0.18)
- pooled: moments 4802, name-days 134, days 63 | net30 -11.0 (t -3.43) | minus control +0.4 (t +0.12); without top 3 name-days TSN 2026-05-04, SKE 2026-06-12, CDNL 2026-08-18: -0.7 (t -0.23)
- first C2 moment per name-day: n 134, days 63 | minus control -1.9 (t -0.16) | net30 -21.1
- at 4 bp: net30 -5.0, minus control +0.4
- prereg (name-day-first) order on this band, for reference: FAIL
    - half A: name-days 55, days 29, moments 2037 | net30 -38.1 bp (t -3.64) | minus control -19.4 bp (t -2.07) | fails
    - half B: name-days 79, days 34, moments 2765 | net30 -44.1 bp (t -5.05) | minus control -29.6 bp (t -3.31) | fails
    - pooled minus control -24.9 bp (t -3.86, 63 days); without top 3 name-days TSN 2026-05-04, SKE 2026-06-12, CDNL 2026-08-18: -27.4 bp (t -4.35)

### C2, RAW $20-100 band
- half A: moments 2037, name-days 55, days 29 | net30 -11.9 (t -2.12) | minus control +0.0 (t +0.01)
- half B: moments 2765, name-days 79, days 34 | net30 -10.3 (t -2.68) | minus control +0.1 (t +0.03)
- pooled: moments 4802, name-days 134, days 63 | net30 -11.0 (t -3.43) | minus control +0.1 (t +0.03); without top 3 name-days TSN 2026-05-04, CDNL 2026-08-18, WLDN 2026-08-07: -1.1 (t -0.37)
- first C2 moment per name-day: n 134, days 63 | minus control -4.6 (t -0.38) | net30 -21.1
- at 4 bp: net30 -5.0, minus control +0.1
- prereg (name-day-first) order on this band, for reference: FAIL
    - half A: name-days 55, days 29, moments 2037 | net30 -38.1 bp (t -3.64) | minus control -21.6 bp (t -2.30) | fails
    - half B: name-days 79, days 34, moments 2765 | net30 -44.1 bp (t -5.05) | minus control -28.2 bp (t -3.42) | fails
    - pooled minus control -25.1 bp (t -4.08, 63 days); without top 3 name-days TSN 2026-05-04, CDNL 2026-08-18, WLDN 2026-08-07: -28.5 bp (t -4.76)

### C2, raw open >= $10, no band (information)
- half A: moments 4222, name-days 108, days 40 | net30 -10.1 (t -2.39) | minus control -0.4 (t -0.11)
- half B: moments 5509, name-days 146, days 40 | net30 -8.5 (t -2.79) | minus control +4.2 (t +1.53)
- pooled: moments 9731, name-days 254, days 80 | net30 -9.2 (t -3.70) | minus control +2.2 (t +0.97); without top 3 name-days TSN 2026-05-04, DELL 2026-09-02, IBM 2026-05-29: +1.4 (t +0.63)
- first C2 moment per name-day: n 254, days 80 | minus control -1.4 (t -0.13) | net30 -25.1
- at 4 bp: net30 -3.2, minus control +2.2
- prereg (name-day-first) order on this band, for reference: FAIL
    - half A: name-days 108, days 40, moments 4222 | net30 -36.0 bp (t -4.77) | minus control -22.5 bp (t -3.59) | fails
    - half B: name-days 146, days 40, moments 5509 | net30 -43.9 bp (t -4.63) | minus control -28.1 bp (t -2.66) | fails
    - pooled minus control -25.3 bp (t -4.14, 80 days); without top 3 name-days TSN 2026-05-04, DELL 2026-09-02, IBM 2026-05-29: -26.4 bp (t -4.35)

