# Catalyst Thrust Family: Open Thrust & Catalyst Shock Study

**Pre-registration:** `docs/studies/catalyst_thrust_family_prereg.json`  
**Standard:** `docs/TEST_STANDARD.md` (EDGE vs PROFITABLE, P2 controls with 11/07 amendment)  
**Completed:** 2026-11-08T07:50:04.599633+00:00  

---

## 1. P4 Canary Verification Suite

| Hypothesis | Planted +8 bp Read | Max CA Shift | Max CB Shift | Row Mismatch | Null Canary Mean (SE) | Null Days | Canary Status |
|---|---|---|---|---|---|---|---|
| **H_OPEN** | +8.02 bp | 0.00 bp | 0.00 bp | 0 | +13.11 bp | 56 | **PASSED** |
| **H_OPEN_GAP8** | +8.03 bp | 0.00 bp | 0.00 bp | 0 | +31.90 bp | 56 | **PASSED** |

---

## 2. Scored Hypotheses Summary Matrix

| Hypothesis | N Trades | Scored Days | Lift vs CA | t-stat (crit) | Half A / Half B Lift | Secondary CB Lift | Winsor A / B | Net @ 12bp | Net @ 16bp | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| **H_OPEN** | 441 | 282 | +17.43 bp | +1.61 (1.97) | +23.1 / +10.7 | +15.53 bp | +29.6 / +23.0 | +1.12 bp | -2.88 bp | **VOID** |
| **H_OPEN_GAP8** | 352 | 233 | +19.68 bp | +1.54 (1.97) | +32.9 / +4.9 | +17.92 bp | +38.9 / +21.8 | +6.67 bp | +2.67 bp | **FAIL** |

---

## 3. Scored Arm Detailed Breakdown

### H_OPEN: VOID
- **Sample Counts:** 441 trades (Half A: 228, Half B: 213) across 282 sessions (Half A: 153, Half B: 129)
- **Primary Control (ca):** Mean -15.65 bp; Unconditional Baseline (BASE): -30.71 bp (Diff: 15.06 bp <= 3.0 bp gate)
- **Lift vs Primary Control:** +17.43 bp (SE: 10.85 bp, day-t: +1.61 vs critical 1.97)
- **Half Consistency:** Half A lift +23.09 bp, Half B lift +10.71 bp
- **Secondary Control Lift (cb, later in session):** +15.53 bp
- **Winsorized Lift (clip top 1%):** Half A +29.59 bp, Half B +22.96 bp
- **Absolute Net Returns:** Mean +1.12 bp (Half A: +6.94 bp, Half B: -5.78 bp); Stress Net: -2.88 bp
- **Final Test Standard Verdict:** **VOID**

### H_OPEN_GAP8: FAIL
- **Sample Counts:** 352 trades (Half A: 178, Half B: 174) across 233 sessions (Half A: 123, Half B: 110)
- **Primary Control (ca):** Mean -13.52 bp; Unconditional Baseline (BASE): -12.05 bp (Diff: 1.47 bp <= 3.0 bp gate)
- **Lift vs Primary Control:** +19.68 bp (SE: 12.79 bp, day-t: +1.54 vs critical 1.97)
- **Half Consistency:** Half A lift +32.90 bp, Half B lift +4.90 bp
- **Secondary Control Lift (cb, later in session):** +17.92 bp
- **Winsorized Lift (clip top 1%):** Half A +38.85 bp, Half B +21.79 bp
- **Absolute Net Returns:** Mean +6.67 bp (Half A: +15.09 bp, Half B: -2.75 bp); Stress Net: +2.67 bp
- **Final Test Standard Verdict:** **FAIL**

---

## 4. Descriptive Information Cells (Pre-Registered)

| Variation / Cell | N Trades | Days | Mean Net @ 12bp | Lift / t-stat | Notes |
|---|---|---|---|---|---|
| **fixed_2m_hold** | 441 | 282 | -0.53 bp | t = -0.05 | Descriptive cell (does not promote verdict) |
| **fixed_3m_hold** | 441 | 282 | -7.94 bp | t = -0.61 | Descriptive cell (does not promote verdict) |
| **fixed_5m_hold** | 441 | 282 | -7.27 bp | t = -0.50 | Descriptive cell (does not promote verdict) |
| **unfiltered_gappers_ratchet** | 1140 | 552 | -8.39 bp | t = +0.47 (lift +3.2 bp) | Descriptive cell (does not promote verdict) |
| **strong_body_ratio_ge_0_5** | 204 | 170 | -25.19 bp | t = -0.73 (lift -10.2 bp) | Descriptive cell (does not promote verdict) |

---
**Operator Summary:** Full study completed under strict causal and anti-peeking guarantees. Zero live parameters modified.
