# Premarket depth test: STOPPED, Databento returned 402 account_insufficient_funds (2026-10-04)

Pre-registration: [`premarket_depth_prereg.json`](premarket_depth_prereg.json) (f7a0e2d). Script:
`tools/studies/premarket_depth_probe.py`. Data (git-ignored, mini only): `data/databento/xnas_premarket_2026-06_08/`.

**Status: no result yet.** The full fetch was stopped at 12:47 ET because Databento refused requests with
`402 account_insufficient_funds`. That happened from the first mbp-10 batch, interleaved with successes, and
even for ~$0.0001 requests. Nothing was scored; a partial sample would not be the pre-registered test.
**Jonathan: please check the Databento Billing page** (credit left, current balance, the $1 usage limit)
before anything else is fetched.

## What was done
- **Universe (free Alpaca SIP, raw/unadjusted bars):** 810 qualifying name-days over 63 sessions (12.9 a day),
  309 with a SIGNAL (premarket-high break), 2,426 CONTROL minutes, 694 name-days at $2–10. 13 name-days have no
  XNAS symbol that day and are excluded, leaving 797.
- **Implementation fix before any scoring:** the first build used Alpaca *split-adjusted* bars (1,047 name-days).
  The pipe check showed XNAS LIMN at $0.14 vs "adjusted" $7.41 (a later 1:50 reverse split). Adjusted prices
  and volumes leak future splits and break the point-in-time $2–20 rule, so everything was rebuilt on raw
  bars (raw daily bars for the prior close and ADV20). About 30% of the adjusted candidates were reverse-split
  artifacts. The adjusted build is kept in `_adjusted_build_discarded/` for audit. No outcome was looked at,
  except the single LIMN smoke entry that exposed the bug.
- **Exact quote (rung 1, mbp-10, windows [qualify−10 min, 09:41]):** **$6.1988** + status $0.0028 = **$6.2016**,
  under the $10 cap. The earlier adjusted-build quote ($6.9449 + $0.0035) is superseded.
- **Pipe check:** ELVA 2026-07-15, $0.0312; XNAS bid/ask matched the SIP closes.
- **Fetched before the stop (quoted cost of requests that returned data):** 106 mbp-10 name-days ($0.7914), 63 status
  days (about $0.004), plus the discarded LIMN smoke file ($0.0026). That is **about $0.80 delivered**.
  `spend_log.csv` shows $3.23 because every attempt, including the 402-refused ones and their retries, is
  reserved conservatively. 72 name-days were refused (×4 attempts each).

## To resume (only after the balance is confirmed)
`WORK=data/databento/xnas_premarket_2026-06_08 /tmp/dbn/venv/bin/python tools/studies/premarket_depth_probe.py fetch`
skips files already present. Then run `score` with the same venv. The $10 cap counts the conservative log
total, so reset or annotate the log only with Jonathan's approval.
