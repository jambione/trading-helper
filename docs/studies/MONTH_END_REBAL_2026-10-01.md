# Month-end rebalancing flow on SPY/TLT (2026-10-01)

> **Skeptic review 2026-10-03** ([SKEPTIC_REVIEW_2026-10-02.md](SKEPTIC_REVIEW_2026-10-02.md) §6): fixed a bug that counted
> the last data bar (2026-09-25) as a month end — a fake −56 bp trade in the holdout and pooled numbers. Rerun: k=1 holdout
> A +36.3 bp (t 2.11, n 25; was +32.7, t 1.93); pre-2016 unchanged (+21.1, t 2.73, 161 months). k=1 was chosen after all four
> periods (including pre-2016) were seen, so it is still a post-hoc pick: **unproven until forward months score**. The
> pilot's market mode is deliberate on paper (no paper auction); its score uses the official crosses.

Pre-registration: `docs/studies/month_end_rebal_prereg.json` (commit 73418b7, before any results).
Script: `tools/studies/month_end_rebal.py`. Data: Alpaca SIP adjusted daily bars 2016-01 → 2026-09-25 (primary, 128 months),
Yahoo adjusted closes 2002-08 → 2015-12 (second out-of-sample, 161 months). Yahoo matches Alpaca on the 2016+ overlap
(daily-return corr 0.996 SPY / 1.000 TLT, median difference 0.15 / 0.31 bp).

**Signal.** S = SPY return minus TLT return from the prior month's last close to the close k sessions before month end.
Trade −sign(S) from that close to the month's last close. If stocks beat bonds, short SPY (or SPY vs TLT); if stocks lagged, go long.
Costs are closing-auction round trips: SPY 2.26 bp, the pair 5.43 bp.

## Bottom line

- **The pre-registered rule FAILS.** At the primary cell (k = 3), validate (2022-06 → 2024-07) is −4.1 bp for SPY-only and +3.2 bp for the pair, and both are below placebo. Nothing promotes.
- **The effect is still the most consistent thing the desk has tested since overnight.** The sign is right in every period and every k: month-end Spearman(S, window return) is −0.12 to −0.56. On the placebo (the same rule at non-month-end dates) it is −0.02 to −0.07, so this is specific to month end, not generic stock/bond reversal.
- **k = 1 (last session only) is positive in all four periods for both SPY-only and the pair:**

| k=1, SPY-only (A) | tune 2016–22 | validate 2022–24 | holdout 2024–26 | pre-2016 | pooled 289 months |
|---|---|---|---|---|---|
| net bp/trade (t) | +15.4 (1.55) | +20.7 (0.95) | +32.7 (1.93) | +21.1 (2.73) | **+20.6 (3.69)** |
| excess vs placebo | +21.4 | +17.3 | +28.9 | +23.4 | +22.8 (3.87) |

- k = 1 was a secondary cell and cannot promote under the rule, so treat it as a post-hoc pick. Three k's were tested; t = 3.7 survives a 3-way Bonferroni correction. 17 of 25 years are positive (k = 3: 19 of 25).
- **Price-pressure signature:** the first 3 sessions of the next month move against the position (pre-2016 pair −66 bp, t −2.6; holdout SPY −86 bp, t −3.4). The pressure partly reverses, which is what a real flow should do, and it means you must exit at the month-end close.

## Economics

- About +20 bp per month on one SPY trade (k = 1), 12 trades a year. That is about 2.5% a year on capital that is in the market only 12 days a year. It is small but cheap to run, and like overnight it fills at the auctions.
- It competes with the overnight book for the same capital at the close on the last day of each month. The flow is decided at the close (MOC by 15:50), so live S has to use the ~15:45 price, not the true close.
- 12 events a year means a live paper test confirms nothing quickly. The history (289 months, two data sources) is the evidence; paper only checks the plumbing.

## Caveats

- Validate (2022-06 → 2024-07, the rate-hike period) is weak at k = 3 and k = 5. The k = 1 result there is positive but t < 1.
- Quarter-end months are not stronger. If anything they are weaker (pre-2016 k = 3 quarter-end −11 bp vs +46 bp for other months). That contradicts the simplest pension story, which predicts bigger quarter-end flows.
- The effect has been published (Harvey et al., 2025). Crowding may erode it.

## Suggested next step

A forward paper trade at k = 1. At ~15:45 on the second-to-last trading day of the month, compute S and submit a SPY MOC order at −sign(S). Exit with an MOC order on the last day. Next event: enter 2026-10-29, exit 2026-10-30. Run it beside the overnight book, not instead of it.
