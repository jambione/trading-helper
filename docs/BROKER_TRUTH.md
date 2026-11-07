# Broker truth (Alpaca paper account), written 2026-11-07 12:06 ET

Source: Alpaca FILL activities and Alpaca portfolio history only. No desk logs, no simulation, no cost assumption
(fills already include the spread). Round trips = buys and sells paired first-in-first-out per symbol.

Fills 322 (short-sale fills 0); round trips 57; sell quantity with no open buy 0 shares; positions still open 0.

| week (Mon) | round trips | win rate | avg win bp | avg loss bp | net bp / trip | realized $ | cumulative $ | Alpaca equity, week end | equity change | overnight trips |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-02 | 57 | 60% | +15.3 | -30.6 | -3.2 | -60.74 | -60.74 | 9,935.38 |  | 0 |

## Earlier weeks: broker fills archived by the desk (the account above was created 2026-11-04)

Validation on the days both sources cover (2026-11-05, 2026-11-06): Alpaca orders 114, archived orders 114, matched 114, only in Alpaca 0, only in archive 0, quantity mismatches 0, worst average-price difference 0.00 bp. If these agree, the archive below is trustworthy.

Archived fills 3004 orders, 2026-09-16 to 2026-11-06; round trips 1467; sell quantity with no open buy 210; still open 6.

| week (Mon) | round trips | win rate | avg win bp | avg loss bp | net bp / trip | realized $ | cumulative $ | overnight trips |
|---|---|---|---|---|---|---|---|---|
| 2026-09-14 | 168 | 36% | +30.6 | -62.2 | -29.1 | -90.22 | -90.22 | 0 |
| 2026-09-21 | 146 | 27% | +81.1 | -63.7 | -24.1 | -155.72 | -245.94 | 0 |
| 2026-09-28 | 282 | 34% | +15.5 | -25.1 | -11.4 | -130.98 | -376.92 | 0 |
| 2026-10-05 | 253 | 39% | +8.4 | -11.7 | -3.9 | -37.16 | -414.08 | 0 |
| 2026-10-12 | 206 | 31% | +8.3 | -11.1 | -5.1 | -37.93 | -452.01 | 0 |
| 2026-10-19 | 153 | 44% | +15.3 | -26.0 | -7.6 | -42.20 | -494.21 | 0 |
| 2026-10-26 | 125 | 54% | +11.3 | -19.0 | -2.5 | -10.58 | -504.79 | 0 |
| 2026-11-02 | 134 | 53% | +12.8 | -25.8 | -5.3 | -98.38 | -603.17 | 0 |

Cross-check: realized $ per week should track the equity change, apart from open positions at week end,
deposits/resets and fees. A large gap in a week means one of the two sources is wrong for that week; look before trusting it.
