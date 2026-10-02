# 12-1 momentum on a fair universe (2026-10-02)

Pre-registration: `docs/studies/momentum_fair_prereg.json`. Script: `tools/studies/momentum_fair.py` (mini, /tmp/lh panel).
Prior: `LONGER_HOLDS_2026-09-27.md` already found no alpha for monthly 12-1 momentum on the ~900-name liquid point-in-time set (OOS alpha +1.4 to +3.3%/yr, t ≤ 0.3). This run checks round-5 T1's own recipe (top 5 of ~100 mega-caps, monthly) on a point-in-time universe.

## Bottom line

**FAILS.** Survivorship was not what made T1 look good. The result is a handful of mania months.

| Top 5 by 12-1 momentum, monthly, net 10 bp | IS 2017–21 | OOS 2022–26 | all 116 months |
|---|---|---|---|
| vs its own top-100 universe (PIT100) | +155 bp/mo (t 1.05) | +182 (t 0.91) | +168 (t 1.36) |
| same on today's survivors (SURV100) | +172 | +182 | +177 |

- **Survivorship lift is only +11 bp/month** (t 1.4). Among the 100 most-traded names, the names that later fell out barely matter.
- **The mean is a few months.** The median month is −5 bp, and only 50% of months beat the universe. Dropping the best 3 of 116 months leaves +55 bp/mo at t 0.5. The best months were Feb 2024 (SMCI, MARA, COIN, NVDA, MSTR: +53%), Apr 2026 (SNDK, LITE, BE, IREN, WDC: +44%) and Jan 2021 (NIO, BLNK, PLUG, MRNA, TSLA: +35%).
- **Beta 1.8 against SPY.** Most of the rest is leverage. The intercept is about +9%/yr, but it comes from those same few months.
- Momentum within the top 100 does better than within the ~900-name liquid set (no alpha there). Universe size is one more free choice, and choosing it after seeing results would be data mining.

## Verdict on the slow-momentum branch

12-1 momentum holds up only as a ranking for **overnight** holds (+16 bp a night, already live). As a monthly position it is a bet on manias with 1.8x market risk, not an edge. Close this branch.
