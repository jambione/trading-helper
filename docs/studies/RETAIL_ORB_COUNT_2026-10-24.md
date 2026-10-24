# Retail order imbalance and stocks-in-play ORB: both stopped UNDERPOWERED at the count step, not scored (2026-10-24)

Both scripts passed two rounds of code review. Their fetches finished overnight with no failures.

## Retail order imbalance (BJZZ sub-penny prints): UNDERPOWERED
- **Count:**
  - 918 / 947 top-quintile trades (12:00 -> close);
  - 37 / 38 days per half (2026-07-01 .. 10-15);
  - placebo MDE **9.5 / 7.9 bp**, above the 5 bp limit.
- **Data checks passed:**
  - the sub-penny share of TRF prints is steady at 21% / 24% / 22% / 20% by month (not a 2x move);
  - all 75 day files are complete.
- **Why it is short of power:** 150 names a day held all afternoon is too noisy for 75 sessions.
- **Not read.** Untested, not failed. A test with real power would need many more sessions.

## Stocks-in-play opening-range breakout (Zarattini et al.): UNDERPOWERED
- **Count:**
  - 1,555 / 818 long trades over 198 / 100 days;
  - halves 2025-04-01 .. 2026-01-13 and 2026-05-26 .. 10-15;
  - control-leg MDE **27 / 28 bp**, above the 10 bp limit.
- **Superset check passed:** the 1,000th name's 20-day median dollar volume was >= $49.5M every day, so the $20M ever-liquid list contains the whole universe.
- **Why it is short of power:** holding the day's most volatile names to the close varies so much (day-level SD ~1.4%) that only a ~27 bp edge per trade could be confirmed.
  - The paper reports Sharpe ratios of 2.4-2.8 over 8 years.
  - Our one year of data cannot confirm or deny that.
- **Not read.** Untested, not failed.
