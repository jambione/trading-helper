# Handoff to Grok: 2026-10-16 ~13:00 ET

Claude is out of usage. Read this file, then `docs/running_list.md` and `docs/PLAN_2026-10-16_bottlenecks.md`. Everything below is committed on `master-mac`.

## Hard rules (unchanged, non-negotiable)

**Accounts and secrets**
- Paper only. Never arm live. Never set `OVERNIGHT_ACCOUNT=live` and never write `config/overnight_live.armed`.
- The repo is public. Never print, log or commit keys, tokens or account numbers. Never read `secrets.json` or `secrets_data.json` to the screen.

**The mini**
- Never `./trading restart` over ssh. The operator restarts from the mini's Terminal.
- Never run pytest on the mini.
- End every ssh command with `< /dev/null`.
- Use `ssh mac-mini-away`; fall back to `mac-mini`.

**Git**
- Stage by explicit path. Never `git add -A` or `git add .`.
- Never stage `tests/test_pctr_line_probe.py`, `tools/pctr_line_probe.py` or `valid_tickers.txt`.
- If a pull on the mini refuses because the tree is dirty, stop. Do not stash, reset or checkout.
- After a test suite, run `git checkout -- config/bot_config.json` if it got dirtied.

**Trading changes**
- No mid-session trading changes without the operator's OK. Ship after the close as one batch.
- No desk restarts 09:25–10:00.
- No spend. The operator declined real-time SIP on 10/16 ("not now"); don't re-pitch it before 10/30.

**Disk and data**
- Check `df` before big replay batches (66 GB was lost to replay temp dirs on 10/16; cleanup is now automatic).
- Daytime Alpaca work goes ONLY through the data-only account: `secrets_data.json` via the `TH_DATA_SECRETS` env var, or the study scripts that read it. Never use the desk's key in market hours.

**Process**
- Pre-register every test (`docs/studies/*_prereg.json`) before scoring.
- Then a result-skeptic review: max 2 rounds, fixes applied exactly.
- Never score a test before it is READY.
- Grade results as PASS / PROMISING / FAIL. PROMISING = right sign in both halves, combined t ≥ 1.5, positive at the measured ~3 bp cost. It earns paper time only, never money.

## Goal and gate (operator 10/16)

The operator wants profit in two weeks.
- **Baseline:** real paper fills 9/28–10/16 = 732 trades, −7.7 bp/trade (t −8), 15 of 15 sessions red. The trend improved from −21 to about −3 to −7 bp from defensive fixes.
- **Midpoint, 10/23:** a week below −3 bp/trade means cut the losing pieces harder.
- **Verdict, 10/30:** about 400 trades can detect +3 bp/trade. If positive, keep building. If negative, recommend the operator stop investing in this intraday-momentum design.
- **Morning scorecard (every day):** yesterday's per-trade mean, opens per 10 min, entry and exit slippage, live `trail_trend_hold` count and outcomes, cumulative vs the gate. Source: `ai_reports/outcomes.jsonl` (fills) and `ai_reports/events.jsonl` (events).

**Core finding 10/16:** the movers screen admits a name *because* it already ran (RPRX: turned 11:45, ran 11:47–11:52, admitted 11:52, bought 11:57 at the top). The screened board fades about −8.6 bp/10m, so every arm inherits the fade. Every entry-timing rule on board names has failed:
- pre-square, ignition, mid-rise, RSI-2, steep turn, oversold-leave;
- confirm-then-buy (10/16);
- sideways filter (10/16).

## Live state

**Shipped:** commit bc491de, live since 09:32:40 10/16. `ai_trail_exit_trend_hold` is on: a trail or leash sale at or below entry is held while the double check's %R is not both-lines-falling.
- Band 0.05%. The hard stop is absolute. It sells if the bar store is more than 300 s stale.
- The first live hold was RPRX at 12:00, which sold green afterwards. It worked as designed.

**Revert rule:** if tonight's rerun fails the prereg rule (beats off in both halves, paired t ≥ 1.5), set `ai_trail_exit_trend_hold` to false in `config/bot_config.json`.

**Off and staying off until a test passes:**
- the no-progress cut;
- the triangle exit (`ai_exit_left_overbought`; the engine-only fix `ai_exit_triangle_engine_only` exists);
- G2 test arms.

The poll profiler (`ai_watch_profile_path`) is still on. Remove it after the loop-caching fix lands.

## Running jobs on the mini (check each, in order)

### 1. Exit sweep, daytime mode on the data account
- Log `/tmp/ecs16n.log`; outputs `/tmp/ecs_20261016n`; sha 8bbede5; 4 replays at nice 19.
- Arms (prereg `docs/studies/exit_cut_sweep_prereg.json` amended_3):

| Arm | What it changes |
|---|---|
| off | old baseline |
| trail_dc | the live hold |
| trail_dc_seat150 | seats wait 150 s before eviction |
| trail_dc_cool30 | re-entry cooldown 30 s instead of 120 s |
| trail_dc_lob | engine-only triangle exit |
| trail_dc_init06 | tighter initial stop, about 0.3% (0.06R) instead of about 0.6% (0.12R). The UPS question |
| trail_dc_np_dc | no-progress cut gated by the double check |
| trail_dc_sqwait0 / trail_dc_sqwait5 | after-square pre-square wait off / 5 min (live 10). ADD after the running sweep finishes: rerun `scripts/exit_cut_sweep.sh /tmp/ecs_20261016n` with the same env plus these two arms in `SWEEP_ARMS` and `SWEEP_SCORE_ARMS`. Finished outputs are skipped. Use the newest sha for the new arms only if `off` and `trail_dc` are rerun at it too; otherwise keep `SWEEP_SHA=8bbede5` (the arm logic is pure config). |

- Expected done around 17:00. It scores itself into `/tmp/ecs_20261016n/result.md` (refs off and trail_dc).
- Per-half split, as done on 10/16 for trail_dc: copy the day files for 10/06–10/09 and 10/12–10/15 into two dirs, then run `.venv/bin/python tools/studies/spread_rvol_score.py --dir <dir> --arms off,trail_dc,<arm> --refs off,trail_dc`.
  - Use `TH_DATA_SECRETS=~/repo/trading-helper/secrets_data.json` before 16:15. The quote cache usually covers it.
- **Decision rules:**
  - New arms vs trail_dc: beat it in BOTH halves, paired t ≥ 1.5.
  - seat150 and cool30 must also not lower opens/10m.
  - trail_dc vs off: the same rule decides keep or revert.
- Winners ship after the close as one config batch. Tell the operator to restart from the Terminal.

### 2. Gap-down short study
- Log `/tmp/gapdown.log`, about 450/2416 days at 12:05 and a few hours more. Prereg `docs/studies/gapdown_short_prereg.json` (amended_1 + _2, READY).
- When the log says done: `cd ~/repo/trading-helper && nice -n 19 .venv/bin/python tools/studies/gapdown_short.py --score`.
- It is resumable: rerun `--minute` if it died. A positive result needs a result-skeptic review before anything.

### 3. Warehouse
- Log `/tmp/warehouse.log`: 120 sessions 2026-04-27..2026-10-15, liquid universe, IEX + SIP 1m bars, written to `ai_reports/warehouse/{iex,sip}/*.npz`.
- Not today. About 6 day-files in 25 minutes while it shares the data account with the gap-down download is roughly 12–16 h for all 240 files. The pre-screen score is tomorrow morning, before the open. It speeds up once gap-down finishes and frees the rate budget. Do not shrink the 120-session window or score a partial set; that needs a new amendment and review. If the download dies, rerun `tools/studies/warehouse_fetch.py --sessions 120 --feeds iex,sip` (it resumes).

### 4. Pre-screen turn test
- Prereg `docs/studies/prescreen_turn_prereg.json`, amended_1 at 5b260f8. **Skeptic round 2: READY.**
- Before scoring, check that the inventory's session list matches the calendar: `_prev` is the previous warehouse FILE, so a missing day-file would mispair warm-up.
- Once the warehouse is complete: `nice -n 19 .venv/bin/python tools/studies/prescreen_turn.py --score`, about 15 min.
- **What it tests:** does a %R turn out of a dip pay on any liquid name before a screen sees it? It grades BOTHUP (the operator's RPRX read) and MID, and must also beat the same-minute cross-name control in both halves.
- This is THE structural test for flaw #1. If it passes or is PROMISING, design a universe scanner to forward-test on paper. If it fails, that is strong evidence for the 10/30 verdict.

### 5. Separate Claude sessions the operator started
- Loop caching for the book loop, 3.7 s vs a 3 s tick. Ship after the close if done.
- 5 pre-existing failing tests (`test_effective_max_positions_scales_with_equity`, 4 in `test_sim_rstop_path.py`).

Check their branches before touching the same files.

## Next items (in order)

1. **Tonight:** score the exit sweep and the gap-down study (items 1–2). Ship sweep winners after the close, and update `docs/running_list.md`. The warehouse and the pre-screen score (items 3–4) wait until the 240 day-files are complete, tomorrow morning before the open.
2. **Cost re-grade** (plan item 2), never done: re-score past gross-positive results at the measured ~3 bp cost in one pre-registered batch with a multiplicity bar.
   - Candidates: SIP volume breakout +9 bp, day-hold pilot, re-entry after a win (t 2.9), needle C2, mid-rise.
3. **Stale-quote guard.** Stop exiting liquid names on IEX trade gaps.
   - Cases: CRH 11:07 10/16 sold after 78 s; EQNR 12:23:59 10/16 sold 18 s after entry (`stale_data`, src `stream_old`, 17.9 s).
   - Code: the blind-book flatten in `ai_positions.py` (~line 7390, `quote_is_live` + `ai_stale_data_max_age_sec`).
   - Fix: judge liveness by NBBO quote age (bid/ask updates) rather than the last IEX trade, and require a longer gap (e.g. 60 s with a REST quote check) before the market flatten, so a truly dead feed is still caught. See memory iex-trade-gaps-are-not-staleness.
   - **Stopgap live since 13:40 10/16** (operator OK): `ai_stale_data_max_age_sec` 15 → 60 s in config (3e0169b, hot-reloaded). After the quote-age fix ships, decide whether to keep 60 or go back to 15.
   - Test, then ship after the close.
4. **Log the hold's decision live.** `trail_exit_trend_hold` counts its reasons only in memory (`DC_STATS`). Log one event per trail sale at or below entry, with the reason (falling / no_bars / stale_bars / green / hard_stop), so the scorecard can show why each sale was or wasn't held. EQNR 12:21 10/16 was a scratch (idle leash + green catch-up overtake to $45.91 over last $45.905), and its hold reason is unknown. Code only; ships after the close.
5. **Morning scorecard script.** Build it if missing; the inputs are above.

## Key files

| File | What it is |
|---|---|
| `docs/running_list.md` | everything open, kept current |
| `docs/PLAN_2026-10-16_bottlenecks.md` | flaws, gates, cadence |
| `docs/studies/` | preregs and results; 10/16 results: CALENDAR_FLOWS (FAIL), CONFIRM_ENTRY (FAIL), SIDEWAYS_FILTER (FAIL) |
| `tools/studies/early_entry_honest.py` | entry-test harness: later-only control, warm lines, PROMISING |
| `scripts/exit_cut_sweep.sh` | replay sweeps; `SWEEP_DAYTIME` + `TH_DATA_SECRETS` for market hours |
