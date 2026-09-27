# Premarket five-pillars study (2026-09-27) — WORK IN PROGRESS

**Status: data collection is not finished yet. There are no results yet, so don't draw any conclusion from this file.**

**Goal.** Find out whether buying five-pillar momentum names premarket (entries 07:00–09:25 ET) captures a move after the real SIP spread. The answer decides whether Alpaca's ~$99/mo paid live SIP feed is worth it.

## Method (implemented in `tools/studies/premarket_five_pillars_study.py`)
- **Candidates come from premarket bars, not daily bars.**
  - Alpaca daily bars are RTH-only: on 2026-03-10, 20 of 133 names had a premarket high above the daily high.
  - `scan`: 30-minute SIP bars from 04:00 to 09:30, every session from 2025-09-02 to 2026-09-25, for every active common stock with a prior close of $1–25 (about 4,000–4,800 names a day).
  - `cands`: premarket high ≥ +10% and premarket volume ≥ 20k gives **11,232 name-days over 248 sessions**. **4,372 of these (39%) are premarket-only movers** that the daily-bar candidate set of the earlier study missed.
- **Premarket RVOL** = cumulative volume since 04:00 ÷ the name's own mean cumulative premarket volume at the same point over the prior 20 sessions.
  - The baseline is interpolated between half-hour boundaries and floored at 5k shares.
  - 302 name-days have no baseline (fewer than 5 prior sessions); they fail the RVOL cells.
- **Minute bars.** `minute`: 1-minute SIP bars from 04:00 to 10:31, reusing `/tmp/fp/min`.
- **Loose set.** `loose`: $2–20, ≥ +10%, cumulative premarket volume ≥ 50k by the 09:25 close gives **5,397 name-days (~21.8 a day)**. This is a superset of every grid cell.
- **News and float.**
  - News: Alpaca/benzinga, from 16:00 the prior day to the decision minute; reused from `/tmp/fp` where possible.
  - Float: current float only, known for 1,570 of 1,612 symbols.
- **Costs.** `quotes`: SIP NBBO median spread in a 10-second window at marks every 10 minutes from 07:00 to 09:20, plus 09:25, 09:35 and 10:00.
  - Entry spread comes from the nearest mark within 10 minutes. Exit spread comes from the nearest mark within 20 minutes, otherwise it falls back to the entry spread.
  - Net = gross − the full quoted spread (half at entry, half at exit) − 1¢ per round trip when the price is under $5.
- **Grid.** Price {$2–20, $2–5, $5–10, $10–20} × gain {10, 20, 30%} × premarket volume {50k, 250k} × premarket RVOL {none, 5, 10} × float {any, <10M} × news {any, yes} = 288 cells.
- **Entries.** Each cell's qualify minute is the first real minute at or before 09:24 where all its conditions hold, point in time.
  - P1: buy the next bar's open.
  - P2: buy 1¢ over the premarket high so far.
  - P3: buy 1¢ over the prior completed 5-minute high.
  - P4: buy the first close back above VWAP after a close below it.
  - RND: all minutes after qualifying (the random baseline).
- **Exits.**
  - Two brackets, each selling in thirds, with a 60-minute cap and the stop checked first inside a bar:
    - +2%/+3%, 2% trail, −5% stop;
    - +$0.05/+$0.08, $0.10 trail, $0.15 stop.
  - Holds of 5, 15 and 30 minutes.
  - Hold to 09:35 and to 10:00.
  - MFE and MAE over 30 minutes.
  - That is 288 × 5 × 7 = 10,080 tests.
- **Validation.** Train on even-index sessions and test on odd ones. The t-stats use per-day means.
- **Fillable size.** Median dollar volume in the 5 minutes after entry, flagged when a $5k order would be more than 5–10% of it.
- **Tradeable names per day.** The report also counts days with ≥3 tradeable names (spread ≤ 100 bp and 5-minute dollar volume ≥ $50k).

## Done as of 11:05 ET
scan, cands, minute, loose, news and float are finished. Premarket quote sampling is about 157 of 248 sessions done: a forward worker (`screen pmdrv`, file `spreads.json`) and a reverse worker (`screen pmrev`, file `spreads2.json`), both niced.

## Partial numbers (plumbing check only, not results)
- 4-session smoke test (2025-10-01 to 10-06): 22 loose names a day, a median premarket spread of about 43 bp at the sampled marks, and 26% of marks had no quote in the 10-second window.
- On those 4 days every P1 exit was net negative, at roughly −110 to −300 bp. **4 days is meaningless statistically.**

## Resume (on the mini)
```
cd ~/repo/trading-helper
screen -ls        # pmdrv / pmrev should have finished (driver.log ends DRIVER_DONE; rev.log stops)
# if a worker died: /tmp/pm/run.sh /tmp/pm/premarket_five_pillars_study.py quotes   (and/or: quotes rev)
/tmp/pm/run.sh /tmp/pm/premarket_five_pillars_study.py analyze > /tmp/pm/an.log 2>&1
/tmp/pm/run.sh /tmp/pm/premarket_five_pillars_study.py report  > /tmp/pm/report.txt 2>&1
```
`/tmp/pm/run.sh` sets `PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies` and runs `nice -n 15 .venv/bin/python`.

Caches in `/tmp/pm`: `scan/`, `cands.json`, `min/`, `loose.json`, `news.json`, `float.json`, `spreads.json`, `spreads2.json`, `grid.pkl`. The study also needs `/tmp/fp` from the earlier five-pillars study.

## Remaining
1. Finish the quotes.
2. Run analyze and report.
3. Write the plain-words answer, the compact table and the SIP recommendation into this file.
4. Add pointers in `docs/NAME_FINDING_DIRECTIVE_2026-09-27.md` and the HANDOFF START HERE block.

Caveats to carry into the final write-up:
- Float is current, not historical.
- The universe is survivorship-biased (currently active symbols only).
- Spreads are 10-minute marks, not exact entry-minute quotes.
- A mark with no quote falls back to the nearest mark.
