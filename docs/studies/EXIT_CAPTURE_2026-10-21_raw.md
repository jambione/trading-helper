# Exit capture: result

Prereg: docs/studies/exit_capture_prereg.json (amended). Live exit code on real fill paths with an injected drift; net = real fill -> bid at exit (spread inside), bp of entry.

Fills: 904 loaded, 851 replayed in every cell; drops {'no_bid_ramp_5_180': 3, 'no_hold_bid_600': 12, 'no_bid_step_10_60': 2, 'no_bid_ramp_0_180': 4, 'no_bid_ramp_20_180': 2, 'no_hold_bid_end': 6, 'no_bid_ramp_5_600': 1, 'no_bid_ramp_10_180': 4, 'no_hold_bid_180': 8, 'no_bid_ramp_20_600': 3, 'no_bid_ramp_-10_180': 3, 'no_bid_ramp_-10_600': 1, 'no_bid_ramp_40_180': 3, 'no_bid_step_20_60': 1}. Config sha cd84f7109ef61ca9, code 2229d1c1 floor2_short: loosen the SIP spread gate to 1% (0.15% is under one cent below $6.67, so no small name could be bought).

**Verdict: KEEPS AN EDGE**

Fidelity (config 4b267f14f8f8, mu = 0 vs live booked): {'n': 123, 'recall': 0.6260162601626016, 'replay_net': -10.087652494412065, 'live_net': -6.271241402856012, 'gap': -3.816411091556053, 'pass': True}

| shape | mu bp | tau s | net (SE) | capture (SE) | capture winsor | win | median hold s | exits |
|---|---|---|---|---|---|---|---|---|
| ramp | 0 | 180 | -8.3 (+1.1) | - (-) | - | 49% | 191.58725881576538 | {'trail': 761, 'dead_trade': 75, 'eod': 15} |
| ramp | 5 | 180 | -3.9 (+1.4) | 0.88 (0.11) | 0.82 | 64% | 170.5009789466858 | {'trail': 805, 'dead_trade': 36, 'eod': 10} |
| ramp | 10 | 180 | -0.5 (+1.3) | 0.78 (0.05) | 0.78 | 71% | 152.3238480091095 | {'trail': 826, 'dead_trade': 15, 'eod': 10} |
| ramp | 20 | 180 | +5.9 (+1.5) | 0.71 (0.04) | 0.71 | 82% | 138.1985080242157 | {'trail': 836, 'dead_trade': 7, 'eod': 8} |
| ramp | 40 | 180 | +19.3 (+1.7) | 0.69 (0.03) | 0.70 | 90% | 133.17479801177979 | {'trail': 845, 'eod': 5, 'dead_trade': 1} |
| ramp | 5 | 600 | -6.0 (+1.3) | 0.46 (0.09) | 0.42 | 60% | 196.16045212745667 | {'trail': 790, 'dead_trade': 48, 'eod': 13} |
| ramp | 10 | 600 | -4.0 (+1.4) | 0.43 (0.06) | 0.43 | 63% | 181.57336711883545 | {'trail': 808, 'dead_trade': 32, 'eod': 11} |
| ramp | 20 | 600 | -1.1 (+1.4) | 0.36 (0.03) | 0.35 | 71% | 160.9685981273651 | {'trail': 824, 'dead_trade': 15, 'eod': 12} |
| ramp | 40 | 600 | +4.5 (+1.2) | 0.32 (0.02) | 0.31 | 79% | 148.57218503952026 | {'trail': 840, 'eod': 7, 'dead_trade': 4} |
| ramp | -10 | 180 | -16.3 (+1.3) | 0.79 (0.06) | 0.82 | 36% | 382.71941089630127 | {'trail': 660, 'dead_trade': 165, 'eod': 26} |
| ramp | -10 | 600 | -13.4 (+1.3) | 0.51 (0.05) | 0.50 | 41% | 223.17893505096436 | {'trail': 703, 'dead_trade': 125, 'eod': 23} |
| step | 10 | 60 | +1.3 (+1.4) | 0.96 (0.05) | 0.97 | 73% | 122.50648188591003 | {'trail': 831, 'dead_trade': 11, 'eod': 9} |
| step | 20 | 60 | +9.5 (+1.7) | 0.89 (0.04) | 0.90 | 85% | 94.42881298065186 | {'trail': 844, 'dead_trade': 5, 'eod': 2} |

Naive capture (exit times unchanged): {180: 0.7537404678257345, 600: 0.43955602941962835}

Break-even entry edge mu* (bp), day-bootstrap 95% interval:

- tau 180 s: mu* 10.816252252366285, CI (7.296926688206636, 15.097429943802808), never-crosses share 0%, live-shifted 5.194013371444637; curve [(0, -8.3), (5, -3.9), (10, -0.5), (20, 5.9), (40, 19.3)]
- tau 600 s: mu* 23.972073177511366, CI (15.52397494695921, 33.465504792229986), never-crosses share 0%, live-shifted 10.60613154879171; curve [(0, -8.3), (5, -6.0), (10, -4.0), (20, -1.1), (40, 4.5)]

Exits vs a plain hold (difference-in-differences, bp; t):

- ramp|5|180|180: -0.6 (t -1.03)
- ramp|5|180|600: -0.6 (t -1.05)
- ramp|5|180|end: -0.6 (t -1.05)
- ramp|10|180|180: -2.1 (t -4.31)
- ramp|10|180|600: -2.2 (t -4.36)
- ramp|10|180|end: -2.1 (t -4.36)
- ramp|20|180|180: -5.6 (t -8.03)
- ramp|20|180|600: -5.6 (t -8.11)
- ramp|20|180|end: -5.6 (t -8.08)
- ramp|40|180|180: -12.1 (t -12.09)
- ramp|40|180|600: -12.2 (t -12.21)
- ramp|40|180|end: -12.2 (t -12.1)
- ramp|5|600|180: +0.8 (t 1.92)
- ramp|5|600|600: -2.6 (t -6.09)
- ramp|5|600|end: -2.6 (t -6.1)
- ramp|10|600|180: +1.4 (t 2.44)
- ramp|10|600|600: -5.5 (t -9.81)
- ramp|10|600|end: -5.5 (t -9.84)
- ramp|20|600|180: +1.3 (t 2.03)
- ramp|20|600|600: -12.4 (t -20.52)
- ramp|20|600|end: -12.4 (t -20.56)
- ramp|40|600|180: +0.9 (t 1.41)
- ramp|40|600|600: -26.4 (t -39.67)
- ramp|40|600|end: -26.5 (t -39.59)
- ramp|-10|180|180: +2.0 (t 3.56)
- ramp|-10|180|600: +2.0 (t 3.6)
- ramp|-10|180|end: +2.0 (t 3.58)
- ramp|-10|600|180: -2.1 (t -4.3)
- ramp|-10|600|600: +4.7 (t 9.62)
- ramp|-10|600|end: +4.7 (t 9.61)
- step|10|60|180: -0.3 (t -0.64)
- step|10|60|600: -0.3 (t -0.64)
- step|10|60|end: -0.3 (t -0.63)
- step|20|60|180: -2.1 (t -2.46)
- step|20|60|600: -2.1 (t -2.46)
- step|20|60|end: -2.1 (t -2.45)

Capture by price at tau 180: {'<$20|10': 0.541864604490968, '<$20|20': 0.32019628206953626, '$20-50|10': 0.7961142301849088, '$20-50|20': 0.7766533106290882, '>$50|10': 0.8829918034046099, '>$50|20': 0.8060555491960718}
