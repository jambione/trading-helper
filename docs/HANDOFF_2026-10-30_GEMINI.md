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

## Running right now (mini, low priority, warehouse only — no Alpaca, no effect on the desk)

1. **`trend_dip`** (operator's 10/17 idea, READY 10/30): `tools/studies/trend_dip_sweep.py --score --workers 2`.
   - Log: `logs/trend_dip.log`; it writes its report itself.
   - Picks one cell on half A and confirms it on held-out B.
   - **Any PASS / PROMISING is PROVISIONAL** until it is rerun on 20+ sessions after 10/15.
2. **`uptrend_pullback`** (the operator's TCOM pattern: slow %R high, fast dips and turns up; READY 10/30). It starts automatically when trend_dip finishes, `count` then `score`.
   - Log: `logs/uptrend_pullback.log`. Results: `ai_reports/uptrend_pullback/{count,result}.json`.
   - Labels: PASS / PROMISING / UNDERPOWERED / FAIL.
   - Information lines include the operator's inverse exit (fast rolling over from the top).
3. **Both:** about 15% PASS each. A PASS/PROMISING goes to the result-skeptic (Mode 3) before anything else.
   - If one passes, it becomes a candidate **second arm** next to the empty square. It does not replace it. See "Multi-arm rule" below.

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
5. **ORB long-history fetch:** `orb_in_play.py --fetch-daily`, `--fetch-open`, `--fetch-day`, then the count.
   - About 35k SDK calls; it runs into the weekend.
   - The count prints only power; `--score` refuses unless it is powered.
   - Score with `--skeptic-ready <round ids> --prereg-commit <sha>` only after the count shows `powered: true`.

Report each verdict the next morning in plain words, with your confidence.

## After the close (to write up for the operator's OK — do not ship without it)

- **Seat-churn plumbing fix.**
  - Names evicted as `never_armable` / `dead_unknown` and later re-seated and bought (about 26 names 10/16-10/29) waste seat time.
  - Write up a fix and its test. The operator wants the plumbing fixed even though it adds no profit on the current signal.
  - Diagnosis is in running list rows 46-47; the script is the scratchpad `seat_why.py` logic, which reads `~/replay_runs/lag_breakdown_recent.json`, the admit ledgers and `events.jsonl`.
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
- **Plumbing:** arm-to-fill is fixed (2.0 to 0.4 min). The remaining delay is the signal's own wait and the seat churn.
- **Sizing:** 3 x 33% only once the book is consistently positive.
- **The never-green cut and the other exit re-tests:** parked behind a Leg-1 edge trigger (row 45).

## Multi-arm rule (operator 10/30)

The desk already supports several arm types (`arm_bucket` per entry); `ai_watch_presquare_only` currently restricts it to the empty square.
- A new arm earns a slot only through its own test (prereg, skeptic, READY), then a paper forward test next to the standard desk with a switch-off rule.
- Switch on one arm at a time.
- Arms compete for the 5 slots by measured results.
- The empty square stays until something beats it.

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
