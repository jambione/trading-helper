# Handoff to Grok (or Gemini) — Fri 2026-10-30 to Tue 2026-11-03

The operator may hand this to Grok 4.7 instead of Gemini; everything below applies to whichever model takes it.

Updated by Claude ~12:15 ET Fri; Claude's usage is running out. The operator decides; you run the work. Read this file, then
`docs/running_list.md` (rows 34-49), `docs/BASELINE_2026-10-30.md`, and `docs/research/LEVER1_REFRAME_2026-10-30.md`.

## The goal (operator 10/30)

- **The goal:** income from a day-trading system the operator trusts. Everything else is a bonus.
- **Horizon:** keep going until the operator is out of ideas. Never recommend quitting. Frame results as "this idea is closed, here is the next one".
- **Style:** day trading only. No desk that holds a few trades for hours.
- **The two levers:**
  1. Catch the first leg (get in sooner).
  2. Keep losers out.

## The standard desk (operator 10/30)

**This week is the baseline:** 10/26-10/29, 101 trades, 59% win, -0.6 bp/trade, improving daily.
- Files: docs/BASELINE_2026-10-30.md, config/baselines/standard_2026-10-30.json, git tag `standard-2026-10-30`.
- Judge every change against it, and pin it as REF in replays.
- It keeps trading unchanged; each session extends its record.
- Today (10/30) its first losers were quiet "tight"-source megacaps (KO, WMT). Nothing changed in config or trading code.

## House rules

- **Mid-session:** no trading changes without asking the operator; the default is after the close. Read-only tools are fine at any time.
- **Study order:**
  1. Write a prereg (`docs/studies/*_prereg.json`) with a written confidence estimate.
  2. The result-skeptic reviews it: two rounds, with fix text applied verbatim.
  3. The operator says READY.
  4. Then build, code-review if the code is new, and run.

  Never change a prereg after seeing results. Positive results go back to the skeptic before anything ships.
- **Alpaca:** one heavy Alpaca job at a time. Historical jobs use the data-only key (`secrets_data.json`). Replays run after hours only. Warehouse-only studies (no Alpaca) may run any time at `nice -n 19`.
- **"Ship it" means:** commit, push, pull on the mini, and restart the desk with `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk`. If a restart breaks the AGY login, alert the operator.
- **The mini cannot `git push`.** Commit there, then push from the MacBook.
- **Live knobs** live in `config/bot_config.json` (hot reload).
- **CI:** the GitHub "Secret Scan" fails on high-entropy strings. Audit false positives into `.secrets.baseline`, or add `# pragma: allowlist secret` (see commit 1b2e4379).

## Done today (10/30), recorded

- trend_dip and uptrend_pullback: both FAIL, both powered (row 54).
- slope-now / pitch timing: CLOSED (row 55).
- Never-green / below-entry cut: CLOSED (row 57).
- EXH-high as an arm: same %R-high family; closed.

## buystop_flow — FIRST, before ORB (running list row 53, operator 10/30 'A')

**Done:**
- Grok committed the 10/24 edits (ab8f24b9).
- Grok applied two skeptic rounds to the power proposal (72b5cfb8).
- **The operator said READY.** The proposal is registered verbatim as prereg `amended_5_power`, sha pinned.

**Next, for Grok:**
1. **Code the ten-day build exactly as the proposal says.** Sessions 2026-04-27..05-08, built through `batch2.t2_step`, in a separate rows file. The frozen keys are untouched, and the half-A cut pairs are frozen.
2. **Code the pinned arm-B cfg.** Start from `config.DEFAULT_CONFIG`, apply `ai_reports/buystop_flow/pinned_bot_config_837dfd79.json`, and never open `config/bot_config.json`.
   - **CAUTION:** `module_shas()` covers `ai_entry_watch.py` and `config.py`, and both changed on 10/30 (the seat-churn knob, bfd928df).
   - The proposal says the frozen sample is NOT rebuilt when `module_shas` changes. Don't let `assert_sample_current` force a rebuild of the frozen 7,975 + 7,975 keys.
   - Handle this as the proposal specifies, and declare it.
3. **Tests, then a result-skeptic code review.**
4. **Fetch the new days** (data-only key, at most 145 trades a day), **then count.**
   - If both halves meet `floors_met`, fetch arm B for the G1-IN set and run `--score`.
   - If not: UNDERPOWERED, final; no more sessions.
5. **Then start ORB:** `nohup scripts/orb_fetch_chain.sh > logs/orb_fetch_chain.log 2>&1 &`.

## Tonight (Fri, after the close) — the operator must start it or tell you to

Start it after the 16:05 snapshot and not before 16:40; its wait check treats any time after 20:00 as done:

```bash
cd ~/repo/trading-helper && nohup scripts/night_2026-10-30.sh > logs/night_2026-10-30.log 2>&1 &
```

It runs, in order:

1. **marketable_limit_hist** (READY): would capping the entry at the desk's own send price + 5 bp have cut losses?
   - Labels: PROMISING / SPREAD-EQUIVALENT / NOT USEFUL / UNDERPOWERED.
   - PROMISING earns a paper forward test using the existing `ai_entry_order_style` limit mode, with pad 0.05.
2. **square_relax fidelity rerun on 10/27.** A FAIL is final (NOT TESTABLE).
3. **iex_order_spread_calibration stage 0.** Expected UNDERPOWERED; if so, fetch no IEX.
4. **square_relax primary, controls and info.** These run only if step 2 passed.
5. **ORB is no longer in this queue.** It runs from `scripts/orb_fetch_chain.sh` AFTER buystop_flow's fetch and count. The steps are the same: `--fetch-daily`, `--fetch-open`, `--fetch-day`, then the count.
   - About 35k SDK calls.
   - The count prints only power; `--score` refuses unless it is powered.
   - Score with `--skeptic-ready <round ids> --prereg-commit <sha>` only after the count shows `powered: true`.

Report each verdict the next morning in plain words, with your confidence.

## After the close (to write up for the operator's OK — do not ship without it)

- **Seat-churn plumbing fix: BUILT** (row 56). The knob `ai_watch_wait_state_evict_sec` defaults to 0, so nothing changes until it's set.
  - **After the close:** pull on the mini and restart the desk (the code needs a restart; at 0 nothing changes).
  - **Turning it on:** propose 600 s to the operator and set it only on their OK.
  - Turning it on ends the standard desk's record and starts a new one.
  - Judge it by turn-to-fill minutes (`tools/studies/lag_breakdown_recent.py`) and per-trade net against the standard desk.
- **Engine-print recording.** The recorder lacks the engine's real-time print times, which weakens replay fidelity. Propose a recording-only change.

## Decided this week (do not reopen)

- **move_to_cost:** NOT SUPPORTED. It was the spread.
- **stale_age:** NOT TESTABLE on the replay.
- **order_spread_replay:** WITHDRAWN.
- **Overnight book and tight-trail nightlies:** RETIRED.
- **first_leg_delay_hist** (VOID: 14.6% missing quotes; strong but unofficial):
  - the empty square has no edge even at the instant it fires (3-min gross -0.2 bp);
  - a 2-5 minute delay costs nothing measurable;
  - 09:45-10:30 entries cost the most.
- **The empty square buys the top of leg 1 by design.** It needs both %R lines high (KO/WMT 10/30: refused `exh_not_tight` through the leg, then bought the top).
- **Slope-now / %R pitch timing:** CLOSED (operator 10/30). Every pitch band was within about 1.5 bp of random on 230k events. Pitch stays logged, for review only (row 55).
- **%R dip / pullback timing:** CLOSED (trend_dip and uptrend_pullback FAIL, row 54).
- **Plumbing:** arm-to-fill is fixed (2.0 to 0.4 min). The remaining delay is the signal's own wait and the seat churn.
- **Sizing:** 3 x 33% only once the book is consistently positive.
- **The never-green cut and the other exit re-tests:** parked behind a Leg-1 edge trigger (row 45).

## Multi-arm rule (operator 10/30)

The desk already supports several arm types (`arm_bucket` per entry); `ai_watch_presquare_only` currently restricts it to the empty square.
- A new arm earns a slot only through its own test (prereg, skeptic, READY), then a paper forward test next to the standard desk with a switch-off rule.
- Switch on one arm at a time.
- Arms compete for the 5 slots by measured results.
- The empty square stays until something beats it.

## Never-green / below-entry cut: CLOSED (row 57, operator 10/30)

The sweep on 10/26-10/30 covered N = 1-10 minutes and both rule shapes: every setting landed within about 1 bp of zero. Don't re-propose early loss cuts.

## Backlog (running list rows 48-49), day trading only

1. **The cost stack:** marketable limit tonight, then buy-stop entry plus order-flow confirmation (both proven small, about +2 bp).
2. **ORB** as an entry signal (this weekend's fetch). If it passes, test an intraday-exit version that is flat before the close.
3. **The %R uptrend pullback** (trend_dip and uptrend_pullback, running now).
4. **Catalyst day trades:** earnings and news gappers, first-pullback entry.
5. **Board ranking:** tight-source megacaps vs movers.
6. **Real-time SIP**, only after a free-data test points there.

Parked with triggers: exit re-tests, a lower price floor, sizing up, the day-hold read (~11/13), the floor-20 window (~Dec).

## Architecture work (running list row 50, operator 10/30)

Ranked:
1. A trustworthy replay: record every live input at the wire.
2. A shared signal-study harness, a nightly warehouse top-up, and a power estimate before each prereg.
3. An arm registry plus seat retention.
4. Split `ai_entry_watch.py` and prune dead knobs.
5. Per-arm exits, including hold-the-leg.
6. A data layer that records each gate's feed source and age.
7. Ops: a safe deploy/restart script and guards on study jobs.

Do #1 and #2 first. Every code change gets a design note, tests and the operator's OK. Trading-path changes ship only after the close.

## Engineering work (running list rows 51-52, operator 10/30)

**Safe to do alone** (no trading-path change). Work on a branch; the operator reviews the PR.
1. **Fill-matched trades table.** Nightly, from Alpaca FILL activities. Reuse `move_to_cost_live.fetch_fills` / `match_fills`.
2. **Health and freshness alarms.** A per-process watchdog, plus data-age alarms with desktop/phone alerts.
3. **Standard-desk scorecard.** Nightly, against the baseline.
4. Study registry.
5. One-page architecture overview.
6. Test-suite health: the flake, CI test runs, speed.
7. Config schema with dead-key warnings.
8. Dead-code removal.

**Needs the operator's OK:**
- the live-money readiness checklist;
- cache and data consolidation.

**Order:** 1, 2, 3 first, then the rest. Each gets a short design note and tests. Never touch `config/bot_config.json` or the trading path without asking. Don't run heavy Alpaca jobs while a study fetch is running.

## Parked re-tests with a trigger (operator 10/30)

Re-test old exit failures on current-system trades ONLY if a Leg-1 result shows a real early edge:
- exit capture;
- holding longer;
- the never-green cut.

Running list row 45.

## Before every preregistration (new, operator 10/30)

Write your expected odds into the prereg (a `confidence` field) before it runs. The operator wants to set expectations. My current estimates (judgment, not measured):

| Item | Confidence |
|---|---|
| square_relax passes fidelity | ~35% |
| …and then shows a benefit | ~25% |
| IEX stage 0 has enough wide orders | ~25% |
| open_spread is a win | ~20% |
| These levers make the book consistently positive within a month | ~15% |

Prefer historical-bar studies (one night) over replay studies (weeks) whenever the question allows.

## Where things are

| What | Where |
|---|---|
| Preregs | `docs/studies/` (square_relax, open_spread_replay, iex_order_spread_calibration, first_leg_delay_hist, move_to_cost_*, stale_age_replay) |
| Study tools | `tools/studies/`; replay harness `tools/replay_session.py` |
| Run outputs (mini) | `~/replay_runs/<study>/` |
| Session snapshots (mini) | `~/session_snapshots/DAY/` (packaged at 16:05) |
| Desk restart | `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk` |
| Live config | `config/bot_config.json` |

Live settings to know:
- arm spread cap 0.15% (16-minute-delayed SIP);
- `ai_eod_liquidate_time` 15:58, `ai_entry_cutoff_time` 15:50;
- slope-now gate off;
- 5 positions × 20%;
- daily loss brake off for paper.
