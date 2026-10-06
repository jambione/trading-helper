# PARKED 2026-10-06 — do not run

Operator corrected the question the same day: day trades, and the gap to resistance is a runway for an open, not an overnight filter and not only a skip. This file is a draft. It has not been scored. Do not run `tools/studies/sr_daily_overnight.py` on the panel until a new prereg says so.

Skeptic review the same day, before any run: **FIX FIRST**. Do not treat a later run of this script as clean. The prior-20-day high excludes today, the 12-1 ratio is `cf[t-21]/cf[t-252]-1`, and the outcome is `open[t+1]/close[t]-1`. Two leaks remain. Location uses `close[t]`, which the live book does not know at the 15:40 plan (MOC cutoff 15:50). The median is taken after dropping names with no next open, so a missing open moves the split. The SPY ship gate checks intercept t only, not intercept > 4 bp. Fix those in a new prereg before any score.

# Pre-registration: daily support/resistance inside the overnight book (2026-10-06)

Machine-readable: [`sr_daily_overnight_prereg.json`](sr_daily_overnight_prereg.json).
**Committed before any location-split outcome is computed.** Queue item #6 in
[`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md).

The 1-minute support and resistance tests asked the scalp to pick a better minute. They failed.
The hold-to-3:55 idea is already accruing as queue #5 and is not scored here. This test asks a
different question: at the close, among the liquid 12-month winners the overnight book already
holds, does it help to keep the names sitting nearer the prior 20-day low and let go of the names
sitting nearer the prior 20-day high?

| Piece | Lock |
|---|---|
| Panel | `~/lh_cache/panel.npz` on the mini, daily bars through 2026-09. No new fetch |
| Book | Top 20 by 12-1 momentum, raw close ≥ $5, ADV20 ≥ $50M. The −1% intraday filter is off |
| Level | Prior 20 sessions only (today's high and low excluded). Location = where the close sits between that low and that high |
| Split | That night's median location. Low side = at or below the median. High side = above it. Need ≥ 16 located names and ≥ 5 per side |
| Outcome | Close → next open, the overnight book's own return |
| Primary | Low-side mean minus high-side mean, one number per night |
| Halves | IS through 2021-12-31, OOS from 2022-01-01. Both must pass |
| Cost | 4 bp auction round trip. It cancels in the swap. The gap still has to clear 4 bp |

**Pass:** both halves, mean gap > +4 bp, night-level t ≥ 2, at least 200 nights. Anything else is
FAIL, or UNDERPOWERED if a half has fewer than 200 nights. A pass is pending skeptic review. It
does not change `bot_config`.

**Ship gate, even after a pass:** regress the nightly gap on SPY's close-to-next-open. If the
intercept t is under 2 in either half, the gap is market beta and does not ship.

**Info only, cannot flip the verdict, cannot be promoted on this panel:** a 60-day window, holding
only the low-location half versus the full top 20, and "closed at or above the prior 20-day high"
versus the other winners. Also report each side's average price and 12-1 momentum.
