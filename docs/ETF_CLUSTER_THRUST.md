# ETF Cluster Thrust (docs/studies/etf_cluster_thrust_prereg.json)

Tests whether a synchronized momentum thrust in sector constituents (>= 2 or >= 3 stocks with res1 >= +15 bp, vol_ratio >= 2.0) 
predicts a forward impulse in the liquid sector ETF (QQQ, SMH, SOXX, XBI, etc.) over H=3 or H=5 minutes.
Standard: docs/TEST_STANDARD.md. 120-session SIP warehouse sample (2026-04-27 .. 2026-10-15), 96 scored sessions.

## Summary of Verdicts

| Hypothesis | K (Stocks) | Hold (min) | Status | Triggers | Lift vs Control | Day t-stat (crit) | Net (after ETF spread) | Stress Net |
|---|---|---|---|---|---|---|---|---|
| K2_H3 | 2 | 3 | **FAIL** | 3677 | -0.83 bp | -4.42 (2.16) | -3.47 bp | -6.63 bp |
| K3_H3 | 3 | 3 | **FAIL** | 1822 | -0.46 bp | -1.34 (2.16) | -3.06 bp | -6.25 bp |
| K3_H5 | 3 | 5 | **FAIL** | 1823 | -0.39 bp | -0.83 (2.16) | -2.62 bp | -5.81 bp |

## Detailed Hypothesis Breakdown

### K2_H3: FAIL
- **Sample size:** n = 3677 (halves: 1945 / 1732), scored days = 96 (halves: 48 / 48)
- **Primary Control Lift:** -0.83 bp (t = -4.42 vs critical 2.16), halves: -0.85 bp / -0.81 bp
- **Winsorized Lift (99% clip):** halves: -1.06 bp / -0.85 bp
- **Secondary Control Lift (Later-only):** -0.42 bp
- **Net Return (after ETF spread):** -3.47 bp (halves: -3.48 bp / -3.46 bp), at 2x stress: -6.63 bp
- **Control Means:** Primary control = -2.62 bp, Secondary control = -3.06 bp

### K3_H3: FAIL
- **Sample size:** n = 1822 (halves: 986 / 836), scored days = 96 (halves: 48 / 48)
- **Primary Control Lift:** -0.46 bp (t = -1.34 vs critical 2.16), halves: -0.08 bp / -0.84 bp
- **Winsorized Lift (99% clip):** halves: -0.25 bp / -0.91 bp
- **Secondary Control Lift (Later-only):** +0.19 bp
- **Net Return (after ETF spread):** -3.06 bp (halves: -2.74 bp / -3.37 bp), at 2x stress: -6.25 bp
- **Control Means:** Primary control = -2.41 bp, Secondary control = -3.19 bp

### K3_H5: FAIL
- **Sample size:** n = 1823 (halves: 987 / 836), scored days = 96 (halves: 48 / 48)
- **Primary Control Lift:** -0.39 bp (t = -0.83 vs critical 2.16), halves: -0.12 bp / -0.65 bp
- **Winsorized Lift (99% clip):** halves: -0.46 bp / -0.78 bp
- **Secondary Control Lift (Later-only):** +0.03 bp
- **Net Return (after ETF spread):** -2.62 bp (halves: -2.39 bp / -2.84 bp), at 2x stress: -5.81 bp
- **Control Means:** Primary control = -2.03 bp, Secondary control = -2.54 bp

