# Handoff to Gemini — Fri 2026-10-30 to Tue 2026-11-03

Written by Claude at ~05:05 ET Fri. The operator decides; you run the work. Read this file, then `docs/running_list.md` (rows 34-44).

## The goal

The operator has two levers:

1. **Catch the first leg (Leg 1).** Remove the delay so the desk gets into a mover's first run, not the tail.
2. **Keep losers out.**

The desk is a paper desk (Alpaca paper) on the Mac mini (`ssh mac-mini`). Recent book: about −5 bp per trade over 350 trades from 10/14 to 10/29.

## House rules

- **No mid-session trading changes without asking the operator.** The default is after the close. Read-only tools are fine at any time.
- **Studies follow one order:**
  1. Write a prereg (`docs/studies/*_prereg.json`).
  2. The result-skeptic reviews it. Two rounds; fix text is applied verbatim.
  3. The operator says READY.
  4. Then build, then run.

  Never change a prereg after seeing results. Positive results go back to the skeptic before anything ships.
- **One heavy Alpaca job at a time.** Historical-quote jobs use the data-only key (`secrets_data.json`). Replays run after hours only.
- **"Ship it" means:** commit, push, pull on the mini, then restart the desk with `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk`. If a restart breaks the AGY login, alert the operator, who restarts it.
- **The mini cannot `git push`.** Commit there, fetch from the MacBook, and push from the MacBook.
- **Live knobs live in `config/bot_config.json`** (hot reload). `signal_engine.env` is not read for them.
- **Keep the running list updated:** `docs/running_list.md`.

## Running right now

**`first_leg_delay_hist`** (READY, operator "Accept and run").
- Started 05:00 ET on the mini. It should finish around 06:25 and stops itself at 09:15.
- Log: `~/repo/trading-helper/logs/first_leg_delay.log`. Result: `~/replay_runs/first_leg_delay/result.json`.
- It answers whether an empty-square entry earns money over 3 minutes at the signal (net of half the spread at entry plus half at exit), and what a 2- or 5-minute delay costs.
- Labels:
  - `signal`: EDGE_AT_SIGNAL / EDGE_BELOW_MARGIN / NO_EDGE_EVEN_AT_SIGNAL / NOT_ABOVE_CONTROL / CONTROL_NEEDED / UNCLEAR
  - `speed`: SPEED_MATTERS / not shown / VOID
- Expected: NO_EDGE_EVEN_AT_SIGNAL. The 10-minute net was already about −5 bp in move_to_cost_hist.
- **If the label is EDGE_AT_SIGNAL or EDGE_BELOW_MARGIN**, send the result to the result-skeptic (Mode 3) before telling the operator it's an edge.
- **Report to the operator:** the label, net(0), the delay costs, and the 09:45-10:30 split. Use plain words and give your confidence.

## Tonight (Fri, after the close) — the operator must start it or tell you to

`scripts/night_2026-10-30.sh` is already on the mini. Start it after the 16:05 snapshot. Do not start it before 16:40: its wait check treats any time after 20:00 as done, so it must run after the nightly job.

```bash
cd ~/repo/trading-helper && nohup scripts/night_2026-10-30.sh > logs/night_2026-10-30.log 2>&1 &
```

It runs, in order:

1. **square_relax fidelity rerun on 10/27.** This is the replay with the held-name fix, 6ddfbfa3.
   - Pass needs code shares within 0.5-2x of live, buy recall ≥ 0.85 and a median offset ≤ 10 s.
   - Expected: ARM_OK about 0.5% (ok). tape_only about 1.75x and wr_no_history about 0.5x are knife edges.
   - **A FAIL is final.** square_relax is then NOT TESTABLE, per its stop rule. Do not change the harness.
2. **iex_order_spread_calibration stage 0.** About 5,500 delayed-SIP reads.
   - It counts how many orders are wide (SIP > 15 bp) after the 16-minute-delayed gate passed them.
   - If half B has fewer than 150 such orders, or they cover fewer than 30 sessions: UNDERPOWERED. Stop, and fetch no IEX.
   - Expected: UNDERPOWERED. The skeptic estimates 65-110.
3. **square_relax primary, controls and info.** These run only if step 1 passed, with stop-at 08:45.

Report each verdict the next morning. Do not interpret square_relax primary results until all phases are done and the reader (`tools/studies/square_relax_read.py`) has run.

## Decided this week (do not reopen)

- **move_to_cost: CLOSED, NOT SUPPORTED** (well powered). The "sideways names lose" pattern was the spread itself. A widest-spread cut beats it.
- **stale_age: CLOSED, NOT TESTABLE.**
  - Its replay judges staleness by print age. Live judges by where the print came from.
  - Its 10/13 fidelity day ran the wrong strategy pins.
  - Live tape_only is only 7.6% of arm checks.
- **order_spread_replay: WITHDRAWN** (underpowered, and cost-only by construction). It was replaced by the IEX calibration.
- **Overnight book and tight-trail nightlies: RETIRED** by the operator. Do not propose overnight work.
- **Sizing:** move to 3 slots × 33% only once the book is consistently positive.
- **Losers by exit (never-green cut):** the operator said "Not now". Parked.

## Next, in order (operator priorities)

1. Read `first_leg_delay_hist`.
2. Read tonight's queue (square_relax fidelity, IEX stage 0).
3. **open_spread_replay** (READY, built, knobs off). It needs a passing REF fidelity on the same harness, so it runs only if square_relax's fidelity passes. Queue it after square_relax finishes.
4. **Board ranking** (no prereg yet). At 09:58 on 10/29 the board held slow megacaps while +5-28% movers waited. Answer first whether steady names suit the empty-square entry better than fast movers.
5. **Premarket T1 simulation** (READY `b46fdc22`). Runs only after the Leg-1 tests have run.
6. **Later:** fix the slope_now replay pitch bug (pitch present 39% in replay vs 70% live); slope-now winners vs losers; lower the price floor; marketable-limit entries; the real-time SIP decision.

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
