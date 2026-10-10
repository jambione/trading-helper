# Workstream U status: historical universe (hist_universe.py), WIP 2026-10-10 ~13:45 ET

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
- `tests/test_hist_universe.py`: 11 tests, all pass. They cover the selectors, the seed and door gates, the timing of the spread and the gap, a no-lookahead check and the scoring.
- `check` mode (`score_day`/`cmd_check`) reports recall against the prereg floors: movers+tight+momentum within 10 min, and traded names (position_shadow), per day and pooled. It also reports precision and the uncovered share.

## Finding that contradicts the brief
- **momentum is NOT rule-based.** It comes from the Discord "[ELITE]" scanner alerts, which go through the dashboard ticker log to `_dashboard_tickers`. It cannot be reconstructed for held-out days.
- momentum stays in the prereg recall denominator. A momentum name counts as recalled only if the movers/tight rules admit it.
- This will likely push prereg recall below 70%. Of the momentum first admissions on 9/24-10/9, 315 of 574 were premarket and 461 had no price.

## Running on the mini (nohup, safe, read-only market data)
- PID 50870: `hist_universe.py daily 2026-01-02 2026-10-09` in `/tmp/ws_u`, log `/tmp/ws_u/daily.log`.
  - It writes `ai_reports/hist_universe/cache/daily_raw_{sip,iex}.pkl` (about 5 min per feed).
- The code in `/tmp/ws_u/hist_universe.py` is a copy of this branch's file.

## Next commands (mini, after the daily cache exists, off-hours)
```
cd /tmp/ws_u && export REPO=$HOME/repo/trading-helper PY=$REPO/.venv/bin/python
nice -n 10 $PY hist_universe.py check 2026-09-24..2026-10-09 > check.log 2>&1      # recall, cached minute bars
# iterate knobs: --set quote_mode=rt / soft_seed=false / gainers_top=N / soft_seed_rth_only=true --out check_x
nohup nice -n 10 $PY hist_universe.py build-range 2026-03-02 2026-09-23 > build.log 2>&1 &   # newest first, skips built days
```
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
