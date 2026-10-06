# IN-SAMPLE, INFO ONLY: trailing-stop exits T1–T4 on the support-arm study data (2026-10-06)

**Already-mined 9/11–10/02 SIP data. Not evidence.** Variants pre-declared in addendum `1bd4659`
([`SR_SUPPORT_HOLD_CLOSE_FORWARD_TRAILS_ADDENDUM.md`](SR_SUPPORT_HOLD_CLOSE_FORWARD_TRAILS_ADDENDUM.md)) to the
forward test (prereg `c1e9c30`), whose primary exit (15:55), controls, cost, sample minimum and pass bar are unchanged.
Script: `tools/studies/sr_support_trails_insample.py` (same arms, seed-37 same-hour random controls and square arms as
`tools/studies/sr_support_arm.py`; reproduces its hold-to-15:55 support − random +29.4 / +20.7 bp). Square = all
square arms (not only matched name-days). Cost 20 bp. Give-back = mean(variant − hold to 15:55) per trade.

**Plain reading (info only):** on this sample every trail gave back more than it saved. The hold-to-close average
lives in a few big afternoon runners (top 10% of trades > 100% of the total), and trails cut those runners. T3
(break-even at +1%, then a 1.5% trail) kept the median and win rate closest to holding and the best support − random
gap among the trails (+16.7 / +13.9 bp), but still averaged 38 / 20 bp less than holding. T1 and T4 lose the edge
over random entirely.


## half A
| exit | arm | n | gross | net | median | win% | give-back vs hold | support − random (t) |
|---|---|---|---|---|---|---|---|---|
| hold 15:55 | support | 342 | +80.9 | +60.9 | +29.3 | 55.6 | +0.0 | +29.4 (t 3.69) |
| hold 15:55 | random | 342 | +51.5 | +31.5 | +15.6 | 52.6 | +0.0 |  |
| hold 15:55 | square | 651 | +48.8 | +28.8 | +13.1 | 53.1 | +0.0 |  |
| T1 trail 1.0% | support | 342 | +20.0 | -0.0 | -22.9 | 42.7 | -61.0 | +2.2 (t 0.26) |
| T1 trail 1.0% | random | 342 | +17.7 | -2.3 | -18.4 | 42.4 | -33.8 |  |
| T1 trail 1.0% | square | 651 | +17.1 | -2.9 | -19.2 | 42.1 | -31.7 |  |
| T2 trail 2.0% | support | 342 | +39.7 | +19.7 | -11.4 | 47.7 | -41.3 | +5.7 (t 0.48) |
| T2 trail 2.0% | random | 342 | +33.9 | +13.9 | -8.1 | 47.4 | -17.6 |  |
| T2 trail 2.0% | square | 651 | +47.5 | +27.5 | +1.6 | 50.1 | -1.3 |  |
| T3 BE at +1%, then trail 1.5% | support | 342 | +43.2 | +23.2 | +24.0 | 54.7 | -37.7 | +16.7 (t 0.97) |
| T3 BE at +1%, then trail 1.5% | random | 342 | +26.5 | +6.5 | +8.8 | 52.9 | -25.0 |  |
| T3 BE at +1%, then trail 1.5% | square | 651 | +30.1 | +10.1 | +4.2 | 51.0 | -18.7 |  |
| T4 block low -0.1%, trail 1.5% from +1% | support | 342 | +28.8 | +8.8 | -24.6 | 33.3 | -52.2 | -2.3 (t -0.16) |
| T4 block low -0.1%, trail 1.5% from +1% | random | 342 | +31.0 | +11.0 | -16.7 | 44.2 | -20.5 |  |
| T4 block low -0.1%, trail 1.5% from +1% | square | 651 | +25.5 | +5.5 | -8.3 | 47.6 | -23.4 |  |

## half B
| exit | arm | n | gross | net | median | win% | give-back vs hold | support − random (t) |
|---|---|---|---|---|---|---|---|---|
| hold 15:55 | support | 823 | +61.4 | +41.4 | +16.9 | 54.6 | +0.0 | +20.7 (t 3.25) |
| hold 15:55 | random | 823 | +40.7 | +20.7 | +8.1 | 53.0 | +0.0 |  |
| hold 15:55 | square | 1158 | +18.8 | -1.2 | -4.4 | 48.7 | +0.0 |  |
| T1 trail 1.0% | support | 823 | +20.4 | +0.4 | -14.1 | 45.8 | -41.0 | +4.6 (t 1.30) |
| T1 trail 1.0% | random | 823 | +15.8 | -4.2 | -18.0 | 40.5 | -24.9 |  |
| T1 trail 1.0% | square | 1158 | +14.0 | -6.0 | -16.9 | 42.6 | -4.8 |  |
| T2 trail 2.0% | support | 823 | +30.1 | +10.1 | +1.2 | 50.3 | -31.3 | +10.7 (t 3.06) |
| T2 trail 2.0% | random | 823 | +19.4 | -0.6 | -12.8 | 47.3 | -21.3 |  |
| T2 trail 2.0% | square | 1158 | +13.1 | -6.9 | -9.8 | 46.8 | -5.7 |  |
| T3 BE at +1%, then trail 1.5% | support | 823 | +41.0 | +21.0 | +13.4 | 54.7 | -20.4 | +13.9 (t 3.29) |
| T3 BE at +1%, then trail 1.5% | random | 823 | +27.0 | +7.0 | +3.6 | 51.6 | -13.6 |  |
| T3 BE at +1%, then trail 1.5% | square | 1158 | +16.2 | -3.8 | +0.0 | 49.5 | -2.6 |  |
| T4 block low -0.1%, trail 1.5% from +1% | support | 823 | +14.4 | -5.6 | -23.1 | 31.3 | -47.0 | +2.1 (t 0.18) |
| T4 block low -0.1%, trail 1.5% from +1% | random | 823 | +12.3 | -7.7 | -24.8 | 40.7 | -28.4 |  |
| T4 block low -0.1%, trail 1.5% from +1% | square | 1158 | +10.5 | -9.5 | -19.0 | 43.9 | -8.3 |  |
