# Workstream U status: historical universe (hist_universe.py), 2026-10-10 ~13:55 ET

Amendment 3 is confirmed (result-skeptic CONFIRM, 2026-10-10). Recall floors are movers+tight only. Do not put momentum back in the denominator, and do not iterate knobs after a recall number exists. The daily cache finished: `ai_reports/hist_universe/cache/daily_raw_sip.pkl` (95M, 13:36 ET) and `daily_raw_iex.pkl` (94M, 13:41 ET), 0 failed. PID 50870 has exited. No calibration `check` and no held-out `build-range` has been run.

# Earlier note, WIP 2026-10-10 ~13:45 ET

## Done
- `tools/studies/hist_universe.py` is written (not yet run end to end). It ports the live admission path at minute resolution:
  - movers_screener: top-N gainers, IEX scan, most-actives, session carry, and the delayed SIP daily-bar measurement
  - the book's movers seed and soft seed
  - tight_screener: scan and seed
  - admit_arm_gates: price band, SIP spread over the 60 s ending 16 min earlier, open-gap block
- Point in time:
  - Bars that close after T are never read; the delayed paths read up to T-16.
  - Daily data comes from prior sessions only. All prices are RAW.
  - Symbol master: Alpaca ACTIVE + INACTIVE, non-OTC, saved in `ai_reports/hist_universe/assets_pit.json` on the mini (15,867 assets, 2,857 inactive). Each day keeps only the names that have a raw SIP daily bar on the prior session.
- Checks against recorded data:
  - The spread statistic matches the recorded `sip_spread` inputs exactly (25/25 on 10/8).
  - The premarket open gap is yesterday's IEX open gap. This matches the live recording (APLD 10/8 -3.21%).
- `tests/test_hist_universe.py` covers the selectors, the seed and door gates, the timing of the spread and the gap, a no-lookahead check, the scoring, `spy_missing_share`, and `file_config`. Full suite on the MacBook, 2026-10-10: 4458 passed, 1 xfailed.
- `check` mode (`score_day`/`cmd_check`) reports recall. The printed prereg count still includes momentum as information. `pass_pooled` is movers+tight only (covered recall, and position_shadow traded names from those sources). The official fidelity-B traded recall uses recorded-replay trades, not position_shadow.

## Finding that contradicts the brief
- **momentum is NOT rule-based.** It comes from the Discord "[ELITE]" scanner alerts, which go through the dashboard ticker log to `_dashboard_tickers`. It cannot be reconstructed for held-out days.
- Amendment 3 took momentum out of the fidelity-B denominator. A momentum name the movers or tight rules also admit is still built. It is not required for the 70% floor.
- Of the momentum first admissions on 9/24-10/9, 315 of 574 were premarket and 461 had no price. That count is recorded-log composition, not a recall rate.

## Daily cache (finished 2026-10-10 ~13:41 ET)
- `hist_universe.py daily 2026-01-02 2026-10-09` wrote the two pickles above and exited. Log: `/tmp/ws_u/daily.log` (`sip: 12899 symbols, 0 failed`, then `iex: 12680 symbols, 0 failed`).
- The code in `/tmp/ws_u/hist_universe.py` is an older copy. The calibration build has to use the committed file, with `cfg_mode=recorded` for the 12 seen days.

## Next (calibration only, one pass, frozen defaults)
```
# after the commit that contains file_config / spy_missing_share is on the mini clone
nice -n 10 $PY tools/studies/hist_universe.py check 2026-09-24..2026-10-09
```
Do not pass `--set`. Do not run `build-range` on 2026-03-02..09-23 until the fidelity gate PASSes and `scripts/hist_sim_batch.sh` is the thing that builds those days.

- Output goes to `ai_reports/hist_universe/DAY.json`; the calibration runs write `calib_DAY.json`.
- `build_log.jsonl` records the universe size and failed-fetch counts. Any day with more than 10% of names failed is flagged `excluded_gt10pct_failed`.
- Expected cost:
  - Minute bars: about 1.5 min per day (1000 symbols took 11 s on SIP and 3.5 s on IEX).
  - Spreads: batched per minute, a few hundred requests per day.

## Open issues / untested
- Never run end to end, so there are no recall numbers yet. Expect runtime bugs on the first `check`.
- Memory: the minute matrices are about 11k symbols x 720 minutes x 5 float64 (~300 MB). Watch this next to the other batches.
- Not modelled:
  - book state (dead_reentry, stale/strike blocks)
  - %R heat relief in rvol_blocks_admit
  - the desk live-quote override (`quote_mode` knob)
  - the soft-seed scout score (approximated by ranking on pct)
  - per-equity dynamic max price
- Alpaca's gainers and most-actives universe is unknown. The current ranking covers every listed symbol in the master.
- Ticker reuse and renames in Alpaca history are not handled.
- `recorded_trades` reads all of position_shadow.jsonl (154 MB), which takes about a minute.
