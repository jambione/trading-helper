# Handoff to Grok: Sat 2026-10-10 → Tue 2026-10-13

Paper day-trading desk. **Focus: day trading only; the overnight book is parked.** The desk loses about 3 bp per trade after a ~3 bp spread, and the goal is to get it into profit. Every idea is pre-registered, reviewed adversarially, and read once. **Never change live trading mid-session.**

## Where things are

- **Repo:** `~/repo/trading-helper` on the MacBook, branch `master-mac`. Push from the MacBook only; the Mac mini cannot push.
- **Live desk:** the Mac mini, reached with `ssh mac-mini-away` (Tailscale has to be connected on the MacBook).
  - Repo on the mini: `~/repo/trading-helper`, Python at `.venv/bin/python`.
  - The live config is `config/bot_config.json` on the mini, and it has **deliberate uncommitted edits. Never `git checkout`/`reset` the mini's tree; only `git pull --ff-only`.**
  - Alpaca data keys exist only on the mini.
- **ssh drops often.** Run anything long on the mini with `nohup … &`, log it to a file, and poll the file.

## Running now (all on the mini, detached)

1. **Counterfactual batch 1** (`/tmp/cf_weekend.sh`): 12 seen sessions (9/24–10/9) × 6 variants. Status is in `/tmp/cf/status.txt` and the report will be `/tmp/cf/REPORT.txt` (due about 14:10 ET Sat).
   - Variants: base, np_lob, np_lob_st, np_lob_nodecay, both_sq, presq_only.
   - **Information only**: these sessions are seen.
2. **Counterfactual batch 2** (`scripts/cf_weekend2.sh`): starts on its own when batch 1 prints ALL DONE. It runs two lanes; status in `/tmp/cf/status2.txt`, report in `/tmp/cf/REPORT2.txt` (due about 20:00–21:00 ET Sat).
   - All arms are presquare-only (the live entry).
   - Entry: %R rising (`pq_wr15`), %R+RSI rising (`pq_wrrsi`).
   - Exits: np_lob, SuperTrend + leash off (`pq_st`), leash off (`pq_nodecay`), and the cross-combos (`pq_wrrsi_st` = the operator's combination).
   - Information only.
3. **RSI history quick look**: done; the result is in `ai_reports/rsi_history/quick_look_2026-10-10.txt`.

## Historical simulator (the main project; the operator said "time is of the essence")

- **Plan:** `docs/studies/HIST_SIM_PLAN_2026-10-10.md`.
- **Prereg:** `docs/studies/hist_sim_prereg.json`. **It is FROZEN; the skeptic gave GO after 2 rounds. Do not edit it.**
- **Idea:** run the LIVE code (`tools/replay_session.py`) on ~140 held-out past days, 2026-03-02..09-23, by synthesizing the recording from a rebuilt watchlist and archived Alpaca data.
- **Scorer:** `tools/studies/hist_sim_read.py` is done and tested.

Two workstreams were in progress in parallel Claude sessions. Each pushed a WIP branch with a status file:

| Workstream | Branch | Status file | Deliverable |
|---|---|---|---|
| U: historical universe | `ws-u-hist-universe` | `docs/studies/WS_U_STATUS.md` | `tools/studies/hist_universe.py`: per-day watchlist + first admission time (movers/tight/momentum), RAW prices, point-in-time symbol master including delisted names; recall check vs `ai_reports/admit_range.jsonl` on 9/24–10/9 |
| R: synthetic-day replay | `ws-r-synth-day` | `docs/studies/WS_R_STATUS.md` | `replay_session.py --universe FILE` for unrecorded days; engine `--engine recompute --engine-bars iex`; fills at the first SIP print AFTER the decision; per-day vacuity log; `tools/studies/synth_day_fidelity.py` |

**Amendment 3 is pending confirmation.** WS-U found that "momentum" admissions are Discord [ELITE] scanner alerts, not a rule, so they can't be rebuilt. Before any recall number existed, the prereg was amended to make the recall floors in fidelity part B cover movers + tight only. A result-skeptic confirmation of amendment 3 is needed before part B runs. If you can't get one, **ask the operator** before running part B. WS-U status: branch `ws-u-hist-universe` at cba0edd (WIP, 11 tests pass, not yet run end to end). A daily raw-bar cache job is running on the mini (PID 50870, log `/tmp/ws_u/daily.log`).

**WS-R state:** branch `ws-r-synth-day` at 45386b1 (WIP; new tests 17 + existing replay tests 19 pass; full suite NOT run). CLI: `replay_session.py --universe FILE [--config-file F] [--equity X]`. It refuses held-out days without `--allow-held-out`, and only the frozen batch may pass that flag. Fidelity tool: `tools/studies/synth_day_fidelity.py export|run|score`. Running on the mini:
- the recorded-replay leg, `/tmp/ws_r_out/run_rec.sh` (PID 49926; status in `/tmp/ws_r_out/rec/status.txt`);
- a synthetic smoke run on 10/08 base (log in `/tmp/ws_r_out/smoke/`).

Known bug: the 9/24 export has a null equity. Next steps are in `docs/studies/WS_R_STATUS.md`: confirm the smoke run has trades > 0 and arm checks > 0, run the synthetic leg, then score.

**Remaining steps, in order. Do not skip the gates.**

1. Finish U and R from their status files. Merge both into `master-mac`, run the full test suite, and push.
2. **Fidelity gate** (prereg `fidelity_gate`), on the 12 calibration days 9/24–10/9 **only**:
   - Variants: base, st, wrrsi_st.
   - Part A: the recorded watchlist through the synthetic path vs the recorded replay.
   - Part B: hist_universe + synthetic vs the recorded replay.
   - Measure the per-day sd and the minimum detectable effect (MDE; `power` in the prereg).
   - Write `/tmp/hs/FIDELITY.json` = `{"verdict": "PASS"|"FAIL", "A": {...}, "B": {...}, "power": {...}}`.
   - **If FAIL: stop and report. Do not loosen anything or change the fill rules.**
3. Only if the gate PASSES:
   - Write `scripts/hist_sim_batch.sh`. It needs the 7 variants from the prereg, with rand_wrrsi two-pass. It must also write `DAY-dayinfo.json` with `spy_missing_share`, `universe` and `fetch_failed`. Use `nice -n 10` and 3 lanes, with outputs in `/tmp/hs/DAY-VARIANT.json`.
   - Run it over 2026-03-02..09-23.
   - Then run `.venv/bin/python tools/studies/hist_sim_read.py --dir /tmp/hs` **ONCE**.
4. Any PASS goes to a result-skeptic review before anything touches config.

## Monday 10/12 is automatic (no action needed)

- The AI hold-check shadow runs at 10:35 ET (LaunchAgent).
- The nightly replays in `scripts/tight_trail_replay.sh` run after the close, including the fresh-session tests:
  - `wr_trend15` + `wr_rand`: `docs/studies/wr_trend_entry_prereg.json`, read at 30 sessions.
  - `np_lob_st` / `np_lob_nodecay`: `docs/studies/supertrend_exit_forward_prereg.json`, read at 45.
- The G2 order-cost read is due 10/15; the day-hold read is due no earlier than 11/13.
- **Do not change the live config.** Monday's fresh-session tests depend on it staying put.

## Hard rules

- **Trading changes:**
  - No live trading or config changes during market hours (09:00–16:30 ET) without the operator's OK.
  - Never place orders or touch the trading API.
  - Never give investment advice.
- **Spending:** no paid data or services (SIP real-time etc.) until the desk is proven profitable.
- **Held-out days:** never compute an outcome on a held-out day (before 2026-09-24) outside the frozen batch → read sequence. That includes no "quick smoke" runs on held-out days; smoke-test on 9/24–10/9.
- **Prices:**
  - Apply price floors/caps and gap % on RAW prices, never split-adjusted ones.
  - Never fill or score at a stale quote or a completed-bar close (the shadow-price bias).
- **Scoring:**
  - Weight each decision equally; never average per name-day first (that was the hindsight artifact behind the room-HOD "pass").
  - Always check trades > 0 / decisions > 0 before believing any replay result. Earlier verdicts were vacuous.
- **AI tools:** never enable agy `read_url` globally, and never run agy over ssh.
- **Restarts:** restarting the desk over ssh breaks Claude/Keychain auth. Don't restart the stack this weekend.

## Report back to the operator

1. Counterfactual REPORT.txt and REPORT2.txt, summarized as information only:
   - $/day and net bp/trade per arm vs reference;
   - all trades vs tight-source trades;
   - whether `pq_wrrsi_st` beats `presq_only`.
2. The status of the fidelity gate and the held-out read.
