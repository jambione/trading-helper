# Calendar flows result 2026-10-16: FAIL (all 9 cells)

Prereg docs/studies/calendar_flows_prereg.json amended_1 (commit f7ce492e, skeptic round 2 READY, committed before scoring).

Closest cells: SPY TOM <2010 +11.6 bp/day (t 2.3) but 2010+ +5.3 (t 1.0), consistent with decay after publication; IWM PREHOL 2010+ +15.8 (t 2.3) but <2010 +6.7 (t 0.6). No cell reaches t 2.6 in either half. Calendar effects beyond month-end are closed for this desk (prereg if_fail).

# Calendar-flow family (Yahoo adjusted closes; prereg amended_1)

t bar 2.6 across YEARS (one difference per year); halves split 2010-01-01; out-of-window = days in no window (month-end excluded); UNDERPOWERED under 8 years or 30 in-window days

| ETF | window | half | years | in-window days | in bp/day | none bp/day | diff bp (t, years) | verdict |
|---|---|---|---:|---:|---:|---:|---:|---|
| SPY | TOM | A <2010 | 17 | 608 | +12.4 | +0.7 | +11.6 (+2.3) | FAIL |
| SPY | TOM | B 2010+ | 17 | 606 | +11.5 | +6.4 | +5.3 (+1.0) | FAIL |
| SPY | QE | A <2010 | 17 | 68 | -13.2 | +0.7 | -13.9 (-1.2) | FAIL |
| SPY | QE | B 2010+ | 17 | 67 | +4.6 | +6.4 | -1.2 (-0.1) | FAIL |
| SPY | PREHOL | A <2010 | 17 | 145 | +7.5 | +0.7 | +6.5 (+0.7) | FAIL |
| SPY | PREHOL | B 2010+ | 17 | 153 | +13.1 | +6.4 | +6.8 (+1.9) | FAIL |
| SPY | OPEX | A <2010 | 17 | 1000 | +6.7 | +0.7 | +5.9 (+1.5) | FAIL |
| SPY | OPEX | B 2010+ | 17 | 987 | +2.2 | +6.4 | -4.1 (-1.1) | FAIL |
| IWM | TOM | A <2010 | 10 | 344 | +3.9 | +1.4 | +5.6 (+0.5) | FAIL |
| IWM | TOM | B 2010+ | 17 | 606 | +9.6 | +6.2 | +3.8 (+0.6) | FAIL |
| IWM | QE | A <2010 | 10 | 39 | +17.5 | +1.4 | +20.1 (+0.7) | FAIL |
| IWM | QE | B 2010+ | 17 | 67 | +11.0 | +6.2 | +3.8 (+0.2) | FAIL |
| IWM | PREHOL | A <2010 | 10 | 85 | +6.6 | +1.4 | +6.7 (+0.6) | FAIL |
| IWM | PREHOL | B 2010+ | 17 | 153 | +21.5 | +6.2 | +15.8 (+2.3) | FAIL |
| IWM | TAXJAN | A <2010 | 10 | 75 | -8.3 | +1.4 | -2.1 (-0.1) | FAIL |
| IWM | TAXJAN | B 2010+ | 17 | 133 | +4.0 | +6.2 | -0.0 (-0.0) | FAIL |
| IWM | OPEX | A <2010 | 10 | 566 | +2.5 | +1.4 | +1.0 (+0.2) | FAIL |
| IWM | OPEX | B 2010+ | 17 | 987 | +1.1 | +6.2 | -5.0 (-1.0) | FAIL |
