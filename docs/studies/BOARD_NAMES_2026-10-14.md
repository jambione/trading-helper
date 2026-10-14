# Board names — FAIL (2026-10-14)

Prereg: `docs/studies/board_names_prereg.json` (result-skeptic READY after 2 rounds, before any score; staged script hash verified on the mini).
Script: `tools/studies/board_names_study.py`. Tests: `tests/test_board_names_study.py`.

## Question
The board fades −8.6 bp per 10 minutes whatever minute is bought. Do four name gates known at the seat minute pick board names that fade less than the ones they exclude, in both halves of 26 sessions?

## Verdict: all four FAIL
| gate | lift vs excluded (A / B) | gated board itself (A / B) | name-day-equal lift |
|---|---|---|---|
| G1 price ≥ $30 | +15.0 (t 1.57) / +5.0 (t 1.70) | −3.8 / −2.1 | +33.7 / +19.6 |
| G2 up ≥ 2.35% by 10:46 | −11.7 / −1.0 | −17.0 / −4.2 | −40.4 / −3.6 |
| G3 above 50-day | −2.8 / +1.0 | −15.3 / −2.5 | +17.5 / +9.3 |
| G4 $500M/day liquid | +10.5 / −1.0 | −3.0 / −4.0 | +29.9 / +8.5 |

## Reading
- **Price ≥ $30 is the one consistent lead.** It is positive in both halves under both weightings, but t < 2 and the gated board still fades (−2 to −4 bp). Pricier names fade less; they do not stop fading. Consistent with cheap-names-carry-the-loss.
- **The runway cell is worse than the rest** (−12 / −1). Names already up 2.35% in the first hour fade harder, the same "seated after the move" pattern as MOMENTUM_RUNWAY_2026-09-27 (pre-seed +37 bp, after seeding ~0).
- **The board's own selection has negative value (information):** refused candidates, scored from refusal to seat or close, minus the board: **+5.4 bp (t 1.90, 20 days)**; refused names average −1.3 bp vs the board's −8.6. The screens pick names that fade more than the ones they turn away.
- Information lines (none can be promoted): momentum source worst in both halves (−36 / −26 bp per 10 min); names seated in the 13:00 hour least bad (+23 / +3); RVOL top tercile and "intraday winner at seat" worse in half A. Each would need its own prereg and forward test.
- SIP minute bars throughout; the live arm reads IEX.

## Erratum (10/14, same day)
The 'pre-seed +37 bp' comparison cited above and in the prereg's prior is hindsight: it is measured only on names that were later seeded, which happened because they rose. It describes the move, not a catchable window. None of this study's verdicts depend on it.

## Raw output
# Board names — 2026-09-08 .. 2026-10-13

1102 board name-days, 26 days, half A ends 2026-09-24. Outcome: gross 10-minute forward return over every board minute, gated names vs the rest (bp per 10 minutes).

| gate | half | gated names | days | in (gated board) | t | lift vs excluded | t | name-day-equal lift | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| G1_price | A | 132 | 13 | -3.8 | -0.64 | +15.0 | +1.57 | +33.7 | FAIL |
| G1_price | B | 392 | 13 | -2.1 | -1.99 | +5.0 | +1.70 | +19.6 | FAIL |
| G2_runway | A | 169 | 11 | -17.0 | -1.96 | -11.7 | -1.16 | -40.4 | FAIL |
| G2_runway | B | 171 | 13 | -4.2 | -2.53 | -1.0 | -0.56 | -3.6 | FAIL |
| G3_above_50d | A | 221 | 13 | -15.3 | -1.80 | -2.8 | -0.21 | +17.5 | FAIL |
| G3_above_50d | B | 290 | 13 | -2.5 | -2.45 | +1.0 | +0.44 | +9.3 | FAIL |
| G4_liquid | A | 60 | 11 | -3.0 | -0.59 | +10.5 | +1.17 | +29.9 | FAIL |
| G4_liquid | B | 194 | 13 | -4.0 | -2.16 | -1.0 | -0.34 | +8.5 | FAIL |

**Verdicts:** G1_price FAIL (unknown 14), G2_runway FAIL (unknown 561), G3_above_50d FAIL (unknown 18), G4_liquid FAIL (unknown 10)

### Information only

- G1_price AND G2_runway: in -4.0 / -4.8, lift +23.5 (t +1.42) / -1.4 (t -0.41), gated names 29 / 102
- G1_price AND G3_above_50d: in +4.7 / -2.3, lift +18.9 (t +1.82) / +2.0 (t +0.87), gated names 67 / 196
- G1_price AND G4_liquid: in +1.4 / -4.0, lift +14.0 (t +1.56) / -0.6 (t -0.28), gated names 47 / 175
- G2_runway AND G3_above_50d: in -44.0 / -4.7, lift -28.4 (t -1.23) / -1.7 (t -0.42), gated names 69 / 83
- G2_runway AND G4_liquid: in -9.0 / -4.7, lift +9.9 (t +0.55) / -1.9 (t -0.92), gated names 15 / 43
- G3_above_50d AND G4_liquid: in +0.2 / -3.8, lift +13.8 (t +1.31) / -0.9 (t -0.34), gated names 40 / 114
- G2 as first written (any seat; out includes afternoon seats): in -24.3 / -4.2, lift -19.6 (t -2.08) / -0.5 (t -0.19), gated names 169 / 171
- gap-down > 1% (gated = gapped down): in -18.5 / —, lift -2.0 (t -0.09) / — (t —), gated names 70 / 15
- intraday winner at seat: in -21.5 / -2.9, lift -26.3 (t -3.28) / +0.4 (t +0.16), gated names 351 / 503
- RVOL top tercile (>= 3.13): in -21.3 / -3.9, lift -23.0 (t -1.90) / -1.6 (t -0.29), gated names 205 / 159
- RVOL bottom tercile (< 1.49): in +12.3 / -2.4, lift +28.5 (t +1.05) / +1.0 (t +0.33), gated names 121 / 243
- seated in hour 9: in -20.6 / -3.2, lift -10.5 (t -1.20) / +0.2 (t +0.07), gated names 144 / 186
- seated in hour 10: in -48.9 / -2.5, lift -37.9 (t -1.58) / +0.6 (t +0.29), gated names 95 / 158
- seated in hour 11: in -7.8 / -6.5, lift +9.4 (t +0.61) / -4.1 (t -1.16), gated names 68 / 102
- seated in hour 12: in -15.0 / -1.5, lift -2.7 (t -0.30) / +1.3 (t +0.24), gated names 47 / 68
- seated in hour 13: in +23.0 / +2.9, lift +44.2 (t +1.10) / +6.4 (t +2.11), gated names 63 / 57
- seated in hour 14: in +10.1 / -46.0, lift +19.4 (t +1.41) / -42.2 (t -1.03), gated names 31 / 37
- seated in hour 15: in +2.7 / +20.0, lift +9.3 (t —) / +23.7 (t —), gated names 26 / 20
- source movers (463 name-days): in -20.7 / -2.5, lift -10.0 (t -0.79) / +1.4 (t +0.46), gated names 131 / 332
- source trending (245 name-days): in +2.8 / -2.9, lift +24.3 (t +1.62) / +0.2 (t +0.08), gated names 115 / 130
- source ? (155 name-days): in — / —, lift — (t —) / — (t —), gated names 155 / 0
- source tight (109 name-days): in — / +0.1, lift — (t —) / +2.0 (t +1.27), gated names 0 / 109
- source momentum (83 name-days): in -36.0 / -25.8, lift -27.7 (t -1.06) / -21.0 (t -0.33), gated names 46 / 37
- source agy (31 name-days): in +7.8 / -17.6, lift +17.2 (t +2.57) / -14.0 (t -0.88), gated names 16 / 15
- G1_price, 30-minute hold: in -18.5 / -5.2, lift +36.4 (t +1.16) / +13.8 (t +1.50), gated names 132 / 392
- G2_runway, 30-minute hold: in -54.1 / -17.6, lift -41.6 (t -1.33) / -10.1 (t -1.95), gated names 169 / 171
- G3_above_50d, 30-minute hold: in -52.3 / -9.6, lift -22.1 (t -0.63) / +0.8 (t +0.19), gated names 221 / 290
- G4_liquid, 30-minute hold: in -5.8 / -11.9, lift +33.1 (t +1.28) / -3.7 (t -0.47), gated names 60 / 194
- refused candidates (never seated) minus board: +5.4 (t +1.90, 20 days); refused mean -1.3 (t -2.34)
- whole board, day-equal: -8.6 (t -2.93)
- split-rescaled name-days (G2 / gap prior close): 14
- None of the information lines can be promoted to a gate; each would need its own prereg.
