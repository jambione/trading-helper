# Last month with Claude: plan (operator 2026-11-04)

Goal: most profit and least loss from this desk, and a project that keeps improving after Claude is gone.
Rule for every session: value per dollar. Short answers, no status polling, batch work into the nightly run, automate before explaining.

## Tier 1: profit and loss levers (in order of evidence per effort)
1. Tonight's restart (11/04 after 16:00): trend-hold fix (held names keep fresh bars; sold 37 of 37 in-band exits the double check said to hold), no_tape fix (one-letter tickers + stream pre-gate), %R slope log.
2. Grid verdict (night_grid_2026-11-04, 19 variants, 20-day confirm): ship every KEEP together under running-list row 82 before the open.
3. AI pick pre-check: today's 09:25 AI picks were all refused by the desk's own gates; publish only picks that pass them.
4. Daily loss brake: ai_daily_loss_limit_r is 1000R (off). Replay a real limit as a grid arm; it caps the bad day without touching entries.
5. "tight" reading in the Gates column (BMNR-style rows looked armable and were not).
6. Cost: spread is ~half of every small loss. Spread-cap arms are in the grid; midpoint exits are only +0.27 bp, skip.
Not worth more time (tested, failed): never-green cuts, never-green entry predictor, wider hold band, seat churn, 09:35 start, 10-min dead trade.

## Tier 2: automation (the desk keeps improving without an AI)
- Standing nightly sweep (LaunchAgent): runs the sweep spec, writes REPORT.md, applies the ship rule, writes ship_candidate.json. One command ships it (scripts/ship_winners.sh).
- Daily scoreboard: rolling 20-session net bp per trade, share of trades over +5 bp, payoff ratio, win rate. Cash comes from net bp per trade, not win rate.
- Desk start at boot: com.jambi.trading-desk has RunAtLoad false, so a reboot leaves it down (11/04).

## Tier 3: handoff
- docs/HANDOFF_FINAL.md: runbook (deploy, restart, reboot, rollback), the rules (no config edits mid-session, pre-registration, two skeptic rounds), where every result lives, how to run and read a sweep.
- Trim MEMORY.md under the 24 KB limit.

## Cost rules for me
- One batch per session, not a conversation per question. Results land in files and the morning report.
- No polling monitors unless a job needs a decision.
- Ask only when the answer changes what I do next.
