# Fibonacci golden pocket, rerun with the corrected controls (fib_pullback_prereg amended_3), one score 11/07

Warehouse SIP bars, 120 sessions, GP trades 1,464 (half A, to 7/29) and 1,026 (half B), all with a primary control. Code passed three reviews before the score (a7127c4a, a5f54cfc, a5377ebb).

| | half A | half B |
|---|---|---|
| Primary control (same minute, 5 other names, bullish and above VWAP) mean | -4.79 bp | -7.63 bp |
| Baseline (same minute, other names, no setup filter) mean | -6.70 bp | -5.10 bp |
| Primary - baseline (the filter effect) | +2.35 bp (t 1.88) | -2.21 bp (t -1.77) |
| Lift of GP over the primary control | **-1.63 bp (t -0.53)** | **+4.27 bp (t 1.92)** |
| Winsorized lift | -2.12 bp | +3.91 bp |
| Secondary control (same name, after the trade) mean | -24.81 bp | -21.13 bp |
| Secondary lift | +39.44 bp | +31.27 bp |
| Net at 5 bp / 20 bp cost | -7.64 / -22.64 bp | -3.69 / -18.69 bp |

- **Prereg verdict: UNDERPOWERED** (neither half reaches +5 bp at t >= 2; half B's upper bound is 8.7 bp so it is not a FAIL).
- **TEST_STANDARD reading: not VOID, no EDGE, not PROFITABLE.** Pooled lift +1.3 bp, day-t 0.69.
- Controls passed the sanity line (primary within 3 bp of the baseline in both halves).
- **The original "+31 / +36 bp, t 8-9" (10/23) was the SECONDARY-style control**: a same-name minute after a swing, which loses 21-25 bp here. Against that control the lift is +31 to +39 bp; against other names at the same minute it is about zero. The original report's own caveat ("the lift mostly measures how badly the control loses") was right.
- Information cells (descriptive): SHALLOW/DEEP/NO_CONFIRM/NO_VWAP/RANDOM_DEPTH are all within a few bp of zero in both halves; win rate 50%.
- SIP bars; the desk trades on IEX. Not shown on IEX.
