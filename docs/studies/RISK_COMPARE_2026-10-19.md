# Risk comparison: the trend exit supports leverage over 26 years, and costs a lot in fast-crash decades (2026-10-19)

Prereg `docs/studies/risk_compare_prereg.json` (description, no word). Script `tools/studies/risk_compare.py`, one run on the mini. SPY daily 1998-06..2026-10 (Yahoo adj close); momentum rows from monthly_book R1. Cash 0%, 1.5x financed at 4%, 2 bp per trend switch.

## 2000-01..2026-09 (S0-S3)

| strategy | CAGR | vol | worst month | worst year | max DD | longest under water | $10k |
|---|---:|---:|---:|---:|---:|---:|---:|
| SPY | +8.2% | 15.1% | -16.5% | -36.8% | -50.8% | 74 mo | $83,070 |
| SPY + 10-month trend | +7.6% | 10.2% | -8.8% | -21.0% | -23.1% | 37 mo | $70,989 |
| 1.5x SPY | +8.9% | 22.9% | -25.8% | -53.8% | -69.5% | 154 mo | $96,795 |
| 1.5x SPY + trend | +9.3% | 15.3% | -13.3% | -30.7% | -33.4% | 37 mo | $108,638 |

## 2017-02..2026-09 (all six, 116 months)

| strategy | CAGR | vol | worst month | worst year | max DD | longest under water | $10k |
|---|---:|---:|---:|---:|---:|---:|---:|
| SPY | +15.1% | 15.5% | -12.5% | -18.2% | -23.9% | 23 mo | $39,018 |
| SPY + trend | +8.6% | 11.8% | -8.8% | -21.0% | -23.1% | 30 mo | $22,141 |
| 1.5x SPY | +19.6% | 23.5% | -20.4% | -29.0% | -35.7% | 24 mo | $56,326 |
| 1.5x SPY + trend | +10.6% | 17.7% | -13.3% | -30.7% | -33.4% | 32 mo | $26,503 |
| R1 momentum | +19.5% | 32.7% | -21.1% | -8.0% | -28.5% | 33 mo | $55,907 |
| R1 + 10% trailing stop | +14.6% | 27.3% | -15.4% | -16.1% | -27.4% | 26 mo | $37,379 |

Plain words: leverage without an exit fell 70% and took 13 years to recover. With the 10-month trend exit, 1.5x SPY beat SPY over 26 years with a smaller drawdown (-33% vs -51%), because it sat in cash through most of 2001-02 and 2008-09. In 2017-2026 the same exit whipsawed (out 2020-04/05, most of 2022, 2025-04/05) and cost 4-6% a year against holding, without cutting the worst drop. The trend exit is crash insurance, paid in fast-recovery decades and collected in slow crashes; two crashes carry the 26-year result. The 10% trailing stop on the momentum book cost 5% a year and did not cut its drawdown. No config change.

## Monthly payout: $10,000 base, take everything above it each month-end, no reserve (operator)

| strategy | 2017-2026 paid | months paid | longest dry run | 2000-2026 paid | longest dry run | lowest balance |
|---|---:|---:|---:|---:|---:|---:|
| SPY | $13,883 | 57/116 | 23 mo | $21,541 | 74 mo | $4,922 |
| SPY + trend | $8,108 | 39/116 | 30 mo | $19,928 | 37 mo | $7,695 |
| 1.5x SPY | $17,790 | 49/116 | 24 mo | $23,302 | 154 mo | $3,046 |
| 1.5x SPY + trend | $10,064 | 30/116 | 32 mo | $24,448 | 37 mo | $6,656 |
| R1 momentum | $21,083 | 30/116 | 33 mo | — | — | $7,147 (ends $7,658) |
| R1 + trail10 | $16,113 | 23/116 | 26 mo | — | — | $7,264 |

Plain words: payouts arrive in bursts; every strategy paid $0 in 2022, and SPY paid nothing from 2001 to 2006. With no reserve a loss comes out of the base and stops the payouts until it is recovered. Skimming pays far less than compounding (SPY: $21.5k paid over 26 years against a $73k gain left invested). Over 26 years the 1.5x trend version paid the most and never went more than 37 months dry; over 2017-2026 it paid less than SPY because of whipsaws. On $10,000 these plans paid about $60-$180 a month on average, before tax.
