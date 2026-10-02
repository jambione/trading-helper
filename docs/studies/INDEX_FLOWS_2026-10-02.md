# Forced index flows: S&P 500 additions and deletions (2026-10-02)

Pre-registration: `docs/studies/index_flows_prereg.json` (commit ed2ddf6, before results). Script: `tools/studies/index_flows.py` (mini).
Events: the Wikipedia S&P 500 change table (revision of 2026-08-10, the last one before the table was removed), effective dates 2016-01 → 2026-08.
Announcements: the first S&P 500 join/replace headline in Alpaca news. Prices: the longer-holds SIP daily panel (active and inactive names). All returns are minus SPY over the same window.

## Bottom line

**All three pre-registered windows FAIL.** The only large, consistent effect is the announcement jump itself, which happens overnight before a regular-session order can be placed.

| Window (net, excess vs SPY) | tune 2016–20 | validate 2021–23 | holdout 2024–26 |
|---|---|---|---|
| W1 add drift: long, first open after the headline → index-fund close | −38 bp (n 59) | +307 (t 2.9, n 27) | +260 (t 1.4, median −129) |
| W2 add reversal: short, index close → +5 sessions | −260 (n 81) | +29 | +6 |
| W3 deletion rebound: long, index close → +5 sessions | +107 (n 37) | **−223 (t −3.3)** | +26 |

- W1 has the wrong sign in tune. Its later means come from a few huge winners (holdout median −129 bp, 41% win).
- W2 and W3 swing sign between periods, and their 20-session versions do the same.
- **The announcement jump** (close before the headline → next open, gross): +109 / +105 / **+428 bp (88% up, t 5.9)** in the three periods. It is real and has grown, but it is priced in overnight. S&P announces about 17:15 ET, and in the 2025 Datadog case the stock was up 11% after hours within the hour. Deletions show no matching drop (−7 / −25 / −43 bp).

## Coverage

242 change events since 2016. Kept: 166 additions and 106 deletions. Dropped:
- 108 because the ticker isn't in the panel (renamed or acquired names);
- 37 with no prices around the effective date;
- 26 with no index-fund volume spike on the trading close, the check that the ticker mapping is right.

Headlines were found for 121 of 166 additions. The median gap from entry to the index-fund close is 4 days.

## What's left

The forced flow is real, but it's priced in when the news hits, not when the funds trade. The only open question is latency: whether any of the jump is left for a buyer who acts in after-hours trading within minutes of the headline. That would need extended-hours minute bars around each headline (available free once more than 15 minutes old) and an after-hours order path. It's a speed contest with professional traders, so it's not a priority.
