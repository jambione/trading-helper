# Indicator confirmation on desk arms: result

Prereg: docs/studies/indicator_confirm_prereg.json. Primary: arm_ok True arms, IEX features, SIP net10 (10 bp), DiD vs the same-name later control. bp per trade.

Days: ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11', '2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24', '2026-09-25', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16', '2026-10-19', '2026-10-20']  excluded: [('2026-09-05', 'load_days: miss iex 100% sip 100%')]
Arm counts: {'arms_A': 354, 'arms_B': 651, 'under_10': 91, 'thin_bars': 138, 'no_price': 2, 'no_control': 1}

## Graded cells

| cell | half | IN | OUT | days | DiD | t | t_crit | lift vs random | OUT lift | IN net | name-day DiD | drop 3 names | drop 3 days | proj. MDE | half |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 177 | 8 | - | - | - | - | - | - | - | - | - | +90.5 | UNDERPOWERED |
| H1 | B | 104 | 452 | 13 | +1.9 | +0.61 | +3.05 | +2.4 | +0.6 | -13.6 | +1.5 | -3.6 | -2.9 | +13.8 | NOT_PASSED |
| H2 | A | 102 | 115 | 10 | -40.6 | -2.18 | +3.25 | -18.2 | +5.2 | -33.4 | -51.1 | -58.7 | -65.4 | +76.3 | FAIL |
| H2 | B | 220 | 336 | 18 | +8.3 | +2.47 | +2.90 | +6.7 | -1.5 | -10.4 | +8.4 | +4.8 | +3.8 | +15.8 | NOT_PASSED |
| H3 | A | 16 | 18 | 2 | - | - | - | - | - | - | - | - | - | +1207.0 | UNDERPOWERED |
| H3 | B | 89 | 79 | 9 | - | - | - | - | - | - | - | - | - | +32.7 | UNDERPOWERED |
| H4 | A | 12 | 22 | 2 | - | - | - | - | - | - | - | - | - | +3144.9 | UNDERPOWERED |
| H4 | B | 58 | 110 | 8 | - | - | - | - | - | - | - | - | - | +39.9 | UNDERPOWERED |
| H5 | A | 13 | 160 | 2 | - | - | - | - | - | - | - | - | - | +2112.9 | UNDERPOWERED |
| H5 | B | 69 | 383 | 12 | - | - | - | - | - | - | - | - | - | +28.2 | UNDERPOWERED |

| cell | verdict |
|---|---|
| H1 | **UNDERPOWERED** |
| H2 | **FAIL** |
| H3 | **UNDERPOWERED** |
| H4 | **UNDERPOWERED** |
| H5 | **UNDERPOWERED** |

A FAIL closes the cell only for effects >= its printed MDE; UNDERPOWERED is no evidence either way.
t_crit uses df = DiD days - 1 per half (stricter than the prereg's ~3.05 / ~2.90 when a cell has fewer DiD days than the half has sessions).

## Information only (cannot pass)

### cells (IEX features, net10)

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1&H3 | A | 4 | 30 | 0 | -46.1 | -2.01 | - | - |
| H1&H3 | B | 31 | 137 | 3 | -2.5 | -0.21 | +15.1 | +1.66 |
| H1&H4 | A | 4 | 30 | 0 | -56.9 | -4.40 | - | - |
| H1&H4 | B | 18 | 150 | 1 | -14.9 | -1.23 | -7.1 | - |
| H1&C1 | A | 1 | 172 | 0 | -90.1 | - | - | - |
| H1&C1 | B | 5 | 447 | 0 | -3.5 | -1.02 | - | - |
| H3&H4 | A | 7 | 27 | 1 | -114.5 | -1.08 | -24.0 | - |
| H3&H4 | B | 43 | 125 | 4 | -12.7 | -1.44 | +7.8 | +1.16 |
| H3&C1 | A | 5 | 26 | 1 | +14.4 | +0.44 | -12.7 | - |
| H3&C1 | B | 19 | 134 | 2 | -23.0 | -1.99 | +7.2 | +0.26 |
| H4&C1 | A | 3 | 28 | 0 | +25.2 | +0.47 | - | - |
| H4&C1 | B | 12 | 141 | 0 | -23.6 | -2.29 | - | - |
| adx_T1 | A | 62 | 155 | 9 | +9.0 | +0.58 | +27.0 | +1.19 |
| adx_T1 | B | 195 | 361 | 18 | -3.6 | -0.99 | -7.5 | -1.94 |
| adx_T2 | A | 73 | 144 | 9 | +6.6 | +0.49 | +23.5 | +1.30 |
| adx_T2 | B | 185 | 371 | 18 | +1.5 | +0.57 | +0.8 | +0.23 |
| adx_T3 | A | 82 | 135 | 9 | -26.5 | -1.70 | -49.1 | -2.54 |
| adx_T3 | B | 176 | 380 | 17 | +10.4 | +2.41 | +8.3 | +2.67 |
| +DI>-DI | A | 168 | 49 | 7 | -0.9 | -0.14 | +26.0 | +1.34 |
| +DI>-DI | B | 497 | 59 | 9 | +1.3 | +0.66 | +3.6 | +0.42 |
| obv_not_at_high | A | 169 | 34 | 4 | -1.8 | -0.14 | -14.4 | -0.55 |
| obv_not_at_high | B | 361 | 168 | 16 | -0.0 | -0.01 | -2.4 | -0.47 |
| ad_not_at_high | A | 169 | 34 | 4 | -1.8 | -0.14 | -14.4 | -0.55 |
| ad_not_at_high | B | 361 | 168 | 16 | -0.0 | -0.01 | -2.4 | -0.47 |
| C1_trend | A | 107 | 66 | 7 | +9.0 | +0.89 | +1.6 | +0.07 |
| C1_trend | B | 349 | 103 | 14 | -1.2 | -0.53 | -3.4 | -0.54 |
| C1_support | A | 94 | 79 | 8 | +17.6 | +1.72 | +21.5 | +1.38 |
| C1_support | B | 382 | 70 | 12 | -0.1 | -0.07 | -6.2 | -0.74 |
| C1_rsi_turn | A | 54 | 119 | 8 | +21.2 | +1.72 | +48.3 | +2.52 |
| C1_rsi_turn | B | 218 | 234 | 18 | -1.3 | -0.51 | -5.2 | -1.04 |
| C1_vol_up | A | 66 | 107 | 8 | -7.4 | -0.60 | -25.7 | -1.81 |
| C1_vol_up | B | 206 | 246 | 18 | -2.6 | -0.87 | -6.7 | -1.57 |
| C1_without_trend | A | 13 | 160 | 2 | +79.9 | +1.32 | -14.3 | -1.20 |
| C1_without_trend | B | 81 | 371 | 14 | -9.3 | -2.52 | -5.7 | -2.27 |
| C1_without_support | A | 17 | 156 | 3 | +69.0 | +1.27 | -29.0 | -1.14 |
| C1_without_support | B | 77 | 375 | 13 | -10.0 | -1.98 | -6.7 | -1.01 |
| C1_without_rsi_turn | A | 28 | 145 | 5 | +5.6 | +0.35 | +11.0 | +0.87 |
| C1_without_rsi_turn | B | 143 | 309 | 15 | -8.1 | -1.86 | -0.9 | -0.26 |
| C1_without_vol_up | A | 28 | 145 | 4 | +65.8 | +1.36 | +19.1 | +0.99 |
| C1_without_vol_up | B | 162 | 290 | 17 | -2.1 | -0.61 | -3.5 | -0.65 |
| adx5_trend | A | 80 | 100 | 9 | -29.3 | -0.80 | -56.6 | -0.95 |
| adx5_trend | B | 127 | 263 | 16 | -7.0 | -1.42 | -10.7 | -1.64 |

### H1-H5, gross10

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 177 | 8 | not scored | | | |
| H1 | B | 104 | 452 | 13 | +2.4 | +0.50 | +1.9 | +0.61 |
| H2 | A | 102 | 115 | 10 | -18.2 | -1.33 | -40.6 | -2.18 |
| H2 | B | 220 | 336 | 18 | +6.7 | +2.17 | +8.3 | +2.47 |
| H3 | A | 16 | 18 | 2 | not scored | | | |
| H3 | B | 89 | 79 | 9 | not scored | | | |
| H4 | A | 12 | 22 | 2 | not scored | | | |
| H4 | B | 58 | 110 | 8 | not scored | | | |
| H5 | A | 13 | 160 | 2 | not scored | | | |
| H5 | B | 69 | 383 | 12 | not scored | | | |

### H1-H5, net5

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 177 | 8 | not scored | | | |
| H1 | B | 104 | 452 | 13 | -2.2 | -0.72 | -3.6 | -1.19 |
| H2 | A | 102 | 115 | 10 | -5.6 | -1.25 | -4.5 | -0.63 |
| H2 | B | 220 | 336 | 18 | +3.8 | +1.45 | +3.7 | +1.30 |
| H3 | A | 16 | 18 | 2 | not scored | | | |
| H3 | B | 89 | 79 | 9 | not scored | | | |
| H4 | A | 12 | 22 | 2 | not scored | | | |
| H4 | B | 58 | 110 | 8 | not scored | | | |
| H5 | A | 13 | 160 | 2 | not scored | | | |
| H5 | B | 69 | 383 | 12 | not scored | | | |

### H1-H5, net30

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 175 | 8 | not scored | | | |
| H1 | B | 103 | 448 | 13 | +2.3 | +0.26 | +1.2 | +0.14 |
| H2 | A | 102 | 113 | 10 | -5.6 | -0.26 | -35.1 | -1.18 |
| H2 | B | 218 | 333 | 17 | +5.5 | +0.99 | +16.2 | +2.78 |
| H3 | A | 16 | 18 | 2 | not scored | | | |
| H3 | B | 89 | 78 | 9 | not scored | | | |
| H4 | A | 12 | 22 | 2 | not scored | | | |
| H4 | B | 58 | 109 | 8 | not scored | | | |
| H5 | A | 13 | 158 | 2 | not scored | | | |
| H5 | B | 69 | 379 | 12 | not scored | | | |

### H1-H5, entry_m2

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 177 | 8 | not scored | | | |
| H1 | B | 104 | 451 | 13 | +3.6 | +0.70 | +1.9 | +0.63 |
| H2 | A | 102 | 115 | 10 | -15.2 | -1.18 | -37.2 | -1.98 |
| H2 | B | 220 | 335 | 18 | +7.0 | +2.97 | +9.3 | +2.70 |
| H3 | A | 16 | 18 | 2 | not scored | | | |
| H3 | B | 89 | 79 | 9 | not scored | | | |
| H4 | A | 12 | 22 | 2 | not scored | | | |
| H4 | B | 58 | 110 | 8 | not scored | | | |
| H5 | A | 13 | 160 | 2 | not scored | | | |
| H5 | B | 69 | 383 | 12 | not scored | | | |

### H1-H5, ctrl_m11_m60

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 40 | 177 | 8 | not scored | | | |
| H1 | B | 104 | 450 | 13 | +0.4 | +0.08 | -1.6 | -0.33 |
| H2 | A | 102 | 115 | 10 | -17.6 | -1.29 | -40.3 | -1.87 |
| H2 | B | 219 | 335 | 17 | +5.0 | +1.43 | +6.3 | +1.60 |
| H3 | A | 16 | 18 | 2 | not scored | | | |
| H3 | B | 89 | 79 | 9 | not scored | | | |
| H4 | A | 12 | 22 | 2 | not scored | | | |
| H4 | B | 58 | 110 | 8 | not scored | | | |
| H5 | A | 13 | 160 | 2 | not scored | | | |
| H5 | B | 69 | 382 | 12 | not scored | | | |

### H1-H5 on SIP features

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 65 | 198 | 7 | not scored | | | |
| H1 | B | 149 | 478 | 16 | +1.5 | +0.46 | +0.8 | +0.17 |
| H2 | A | 132 | 131 | 11 | +11.2 | +0.67 | +21.2 | +0.85 |
| H2 | B | 314 | 313 | 17 | +6.0 | +2.65 | +9.8 | +2.94 |
| H3 | A | 33 | 13 | 2 | not scored | | | |
| H3 | B | 125 | 91 | 13 | not scored | | | |
| H4 | A | 29 | 17 | 2 | not scored | | | |
| H4 | B | 105 | 111 | 11 | not scored | | | |
| H5 | A | 17 | 213 | 4 | not scored | | | |
| H5 | B | 96 | 457 | 13 | not scored | | | |

### H1-H5, all arm rows (refusals included)

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 238 | 1068 | 12 | not scored | | | |
| H1 | B | 693 | 3543 | 18 | -2.9 | -1.03 | -2.6 | -0.87 |
| H2 | A | 706 | 600 | 13 | -4.8 | -1.07 | -2.6 | -0.29 |
| H2 | B | 2262 | 1974 | 18 | -0.6 | -0.68 | +0.6 | +0.25 |
| H3 | A | 77 | 61 | 7 | not scored | | | |
| H3 | B | 180 | 164 | 18 | not scored | | | |
| H4 | A | 54 | 84 | 6 | not scored | | | |
| H4 | B | 122 | 222 | 15 | not scored | | | |
| H5 | A | 49 | 1025 | 9 | not scored | | | |
| H5 | B | 143 | 3197 | 18 | not scored | | | |

### H1-H5, paper fills

| cell | half | IN | OUT | days | lift vs random | t | DiD | t |
|---|---|---|---|---|---|---|---|---|
| H1 | A | 42 | 171 | 6 | not scored | | | |
| H1 | B | 131 | 543 | 13 | +6.5 | +1.23 | +8.0 | +1.82 |
| H2 | A | 102 | 111 | 8 | -4.1 | -0.24 | -54.3 | -2.51 |
| H2 | B | 277 | 397 | 18 | +3.3 | +1.54 | +3.7 | +0.96 |
| H3 | A | 16 | 21 | 3 | not scored | | | |
| H3 | B | 111 | 93 | 8 | not scored | | | |
| H4 | A | 14 | 23 | 3 | not scored | | | |
| H4 | B | 68 | 136 | 9 | not scored | | | |
| H5 | A | 13 | 160 | 2 | not scored | | | |
| H5 | B | 81 | 484 | 15 | not scored | | | |

Controls in m+1..m+10 (overlapping the arm's hold): 20.5%

Video setups: {'arm_namedays_10_30_14_30': 392, 'fetch': "not done: needs the operator's OK (prereg)", 'scored': False}
