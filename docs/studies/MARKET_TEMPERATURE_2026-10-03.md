# Market temperature: trade only on hot mornings? (2026-10-03)

Pre-registered: `market_temperature_prereg.json` (5eb0044, before any data). Data: `tools/studies/allsym_daily_fetch.py`
(5,477 US common stocks with bars, active and inactive, SIP daily, 2025-09..2026-10). Script:
`tools/studies/market_temperature_study.py`. T20 = stocks (prior close >= $1, prior-day $vol >= $250k) gapping >= 20% at
the open; terciles fixed on the first half (COLD <= 5, HOT >= 8).

| half | temp | days | desk-like o->c bp | Bob gappers o->c | Bob gappers o->high |
|---|---|---|---|---|---|
| first | HOT | 47 | -135 | -245 | +2160 |
| first | COLD | 44 | +14 | -478 | +1879 |
| second | HOT | 49 | -3 | +19 | +2915 |
| second | COLD | 41 | -49 | -177 | +2445 |

- desk HOT - COLD: **-148 bp (t -2.75)** first half, +46 (t 0.74) second half. Bob HOT - COLD: +233 (t 0.95), +196 (t 0.54).
- **Verdict: FAIL** on (a) the desk decision and (b) Bob's claim for his own universe. Hot mornings were significantly
  *worse* for $10+ momentum names in the first half and indistinguishable in the second.
- Gappers fade open->close in almost every cell but offer +1,900..+2,900 bp of open->high runway: movement, not
  direction, like the runway studies. The desk's own days since 9/16 show no temperature pattern.
- Caveat: measured at the 09:30 open, not 7 AM premarket; whole-session holds.
