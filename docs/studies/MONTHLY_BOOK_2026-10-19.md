# Monthly book: five price-only rules, all FAIL against SPY (2026-10-19)

Prereg `docs/studies/monthly_book_prereg.json` (amendments a and b; result-skeptic Mode 1 READY). Script `tools/studies/monthly_book.py --score`, one run on the mini. daily.pkl, 2016-01-04..2026-10-15, 3,640 ever-liquid names including delisted ones; 116 holding months (A 2017-01..2021-11, 59; B 2021-12..2026-08, 57). Top 20, equal weight, month-end close to month-end close, 10 bp per side. Survivorship passes: 11-15% of eligible names died each year 2017-2019; 24 series excluded for holes.

**R1_smooth_momentum: FAIL | R2_near_52w_high: FAIL | R3_low_volatility: FAIL | R4_trend_low_vol: FAIL | R5_one_month_reversal: FAIL**

| rule | net - SPY / mo | alpha / mo (t), pooled | beta | alpha without 3 best months | MDE |
|---|---:|---:|---:|---:|---:|
| R1 smooth momentum | +0.65% | +0.33% (+0.45) | 1.25 | -0.02% | 1.41% |
| R2 near 52-week high | -0.79% | -0.39% (-1.12) | 0.69 | -0.49% | 0.67% |
| R3 low volatility | -1.03% | -0.25% (-1.36) | 0.39 | -0.33% | 0.36% |
| R4 trend + low vol | -0.85% | -0.11% (-0.54) | 0.42 | -0.16% | 0.39% |
| R5 one-month reversal | -1.49% | -2.25% (-3.13) | 1.59 | -2.66% | 1.38% |

SPY averaged +1.28% a month over the sample. The equal-weight universe's alpha was -0.43% a month.

Information rows (not in the word): a 10% stop from entry or a 10% trailing stop, sold at the next open, did not turn any rule positive. R1 trail10 alpha +0.29% (vs +0.33%), worst month -15.4% (vs -21.1%); stop10 -0.10%.

Plain words: none of the rules beat SPY by more than its market risk. R1 earns more than SPY by carrying beta 1.25 and catching mania months (2020-10, 2026-01, 2026-03); without its three best months its alpha is zero. The low-volatility rules lag a market that rose about 16% a year and earn no risk-adjusted premium. The one clear result is negative: last month's biggest losers keep losing (R5, t -3.1), which is momentum seen from the other side. "Avoid last month's losers" is a post-hoc idea from this result and would need its own prereg on the untouched 2000-2015 data. Stops shrink the worst month but add no return. Monthly price-only stock picking on this sample does not beat holding SPY.
