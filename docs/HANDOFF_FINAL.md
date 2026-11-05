# Trading desk handoff (written 2026-11-05, for whoever runs this next: operator, Gemini, Grok or another assistant)

Read this first, then `docs/SCOREBOARD.md`, then `docs/running_list.md` (newest rows at the bottom).

## What this is
A paper day-trading desk on Alpaca (account reset to $10,000 on 11/04). Free IEX data feed. Code in `~/repo/trading-helper`, branch `master-mac`.
The live desk runs on the Mac mini: `ssh mac-mini-away` (Tailscale; fall back to `ssh mac-mini`). The MacBook has code only; only the mini reaches the broker.
Current live shape: 3 slots x 33% of equity (`ai_max_positions` 3, `ai_max_position_pct` 33, `ai_risk_pct` 1.65), daily loss brake 1R (`ai_daily_loss_limit_r` 1.0, blocks new entries, never sells), all positions flatten 15:50 ET.

## The honest state of things
- Rolling 20 sessions to 11/04: 720 trades, **net -8.0 bp per trade** (SE 1.4), win 34%, never-green 42%, -$131. The desk loses money. Every entry filter, exit shape, and "sideways/range/overbought" idea tested since September failed a pre-registered test (see memory notes and running list). Do not assume an edge exists.
- Never-green trades (never reach entry +5 bp) are the whole loss, but they cannot be identified at entry or cut early without costing more than they save (all tested, all failed).
- High-confidence actions are risk and ops hygiene, not profit finding. Anything that "improves" a number on a few days needs the process below.

## Daily routine (5 minutes)
1. `python3 tools/daily_scoreboard.py` on the mini (also runs nightly from `scripts/nightly.sh`, writes `docs/SCOREBOARD.md`). Judge on the rolling-20 line, never one day.
2. `./trading status` on the mini: dashboard, engine, discord, trending, movers, tight, ai_trader, watchdog, snapshot, tunnel all up. If any is down after a reboot: `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk` **from the mini's GUI session or ssh kickstart, never `./trading restart` over plain ssh** (an ssh-started process holds the port and breaks the LaunchAgent; Claude/AGY auth also dies). The agent has RunAtLoad true since 11/05, so a login should start the desk.
3. Before the open: first size_plan about 33% of equity; check `logs/watchdog.log` tail. The hourly `instrumentation_check NONZERO` line before ~09:45 is a false alarm (it expects trade rows from 04:00).

## Changing anything (the rule that kept this honest)
- Config knobs live in `config/bot_config.json` (hot-reloaded; `load_config()` reads only this file, never `signal_engine.env`). Code changes need a desk restart.
- Test before shipping: `tools/sweep_replays.py run <spec.json> --stage both` on the mini (specs in `docs/sweeps/`; results in `~/sweeps/<name>/REPORT.md` and `report.json`). SCREEN keep = mean day delta > 0 and >= 60% of days better; CONFIRM = day-t >= 1.5 and both halves positive. `tools/ship_winners.py <report.json> <spec.json>` prints what the rule would ship; `--apply` edits config; a human reviews and commits.
- New ideas get a pre-registration in `docs/studies/*_prereg.json` BEFORE any scoring, a skeptic review, one read, no retuning. Check `docs/studies` and the memory notes before calling anything "untested".
- Replays only run outside 09:00-16:30 ET (Alpaca request budget). Full pytest rewrites `config/bot_config.json`: restore with `git checkout -- config/bot_config.json`, never `git add -A`. 7 known failing tests: test_ai_trading::test_effective_max_positions_scales_with_equity, test_arm_path_state::test_no_new_state_is_written_on_the_arm_path, four in test_sim_rstop_path, test_stale_age_knobs::test_arm_ceiling_releases_a_20s_print_but_not_a_40s_one.
- The mini cannot `git push`: commit on the MacBook, push, then on the mini `git fetch` and `git checkout origin/master-mac -- <files>` (or pull at a planned restart).

## Pending / scheduled as of 11/05
- Overnight grid (19 variants) finishes by 07:45; ship decisions per the rule above. `leash10` (decay idle 8 -> 10 s) was the lone strong screen result.
- Triangle exit test (`docs/sweeps/triangle_engine_2026-11-05.json`) runs after the 11/05 close; result in `~/sweeps/triangle_engine_2026-11-05/REPORT.md`. Prior is low.
- Forward flat-entry rule (slope log): read at 150 forward entries; rule fixed in running list row 85.
- The mini has not pulled the premarket merge (`63dd1050`); Phase B stays dry-run until real-time SIP is bought. SIP switch steps: `docs/SIP_DAY1_RUNBOOK.md`.
- Deliberately closed (do not rebuild): never-green cut/predictor, midpoint exits as a profit source, sideways-shape and range filters (11/05), support-bounce range trade, range-arm retro (frozen), premarket on IEX.

## Where things are
`ai_entry_watch.py` arm/seat/admission gates; `ai_positions.py` sizing, trail, exits; `config.py` defaults; `tools/watchdog.py` process supervision; `tools/replay_session.py` replays; `tools/studies/` one-off studies; `docs/LAST_MONTH_PLAN_2026-11-04.md` the plan this handoff closes; Claude memory notes in `~/.claude/projects/-Users-jonathanbrasfield-repo-trading-helper/memory/` (index `MEMORY.md`).
