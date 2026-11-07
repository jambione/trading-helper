# Experiment board (generated from docs/experiments/registry.json, 2026-11-07)

Pipeline: idea -> prereg -> skeptic -> tool -> smoke -> data_check -> running -> verdict. Edit the registry, not this file; run `tools/experiments.py board`.

## Counts

| status | n |
|---|---|
| idea | 10 |
| queued | 1 |
| running | 9 |
| collecting | 5 |
| blocked | 3 |
| open | 5 |
| verdict_fail | 10 |
| underpowered | 2 |
| void | 1 |
| done | 3 |
| parked | 1 |

## Needs attention

- **N1** [blocked] V1 volume-clock %R - First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. Next: Read the rerun (~/bsf_run2.log); run a second sanity check on the control means before any verdict. (blocked on: rerun with fixed controls)
- **N3** [blocked] W opening washout - First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. Next: Read the rerun (~/bsf_run2.log); run a second sanity check on the control means before any verdict. (blocked on: rerun with fixed controls)
- **N4** [blocked] D dwell/knock - First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. Next: Read the rerun (~/bsf_run2.log); run a second sanity check on the control means before any verdict. (blocked on: rerun with fixed controls)
- **L1** [open] stop built from planned entry, not the fill - Slippage since 10/26: mean +4.5 bp, median +2.3, 30% beyond 5 bp (n 1747) on a ~60 bp stop. Next: Candidate live change; needs the operator's say.
- **L2** [open] pre-entry prints can set the peak and arm breakeven - Plausible; '64 prints on tick 1' not confirmed in code. Next: Check against logged peaks before touching anything.
- **L3** [open] live clamps ai_watch_synth_rr at max(0.25) - Verified: targets under +125 bp cannot be set live. Next: Needed only if X9 passes.
- **L4** [open] vacuity guard for sweeps - Replay arms can silently not run (X1/X3 floor was a no-op). Next: Add one-day smoke check to sweep_replays.py.
- **P1** [open] audit control construction in every random-minute study tool; add control-sanity data gate - bar_signal_family's one-sided exclusion inflated same-name controls (+11.6/+27.9 bp vs -7.5 bp for random minutes). board_structure_family (H1/H2) is symmetric (verified). ~20 other tools use random-minute controls and are unaudited: analyst_upgrade, book_walls, clock_windows, daily_filter_screen, early_entry_honest, first_presq_adversarial_argon, five_pillars_study, gate_counterfactual, momentum_alert_markouts, limit_below_study, momentum_runway_study, range_arm_control, runway_target_study, sector_study, sip_breakout_study and others. AUDIT RESULT (subagent + Argon, 11/07): none has the exact one-sided bug, but the stricter point holds - any same-name control drawn BEFORE the trigger sits on the path that made the stock fire. Flagged: sr_* family (5 tools, same-hour pool excluding only the trigger minute), range_arm_control (600 s exclusion vs 1800 s hold), premarket_depth_probe, sip_breakout_study, fib_pullback, structure_pullback, book_walls (300 s vs 600 s horizon), desk_vs_random/five_pillars/momentum_runway (full-day same-name), and board_structure_family H1/H2 (controls reach t-30..t-16). Clean: strictly-later or same-minute cross-name controls (early_entry_honest, limit_below, wr_trend_trade, etf_bars, first_presq_adversarial, analyst_upgrade, regime_beta_family B2 horizons). Next: Standard (P2). Rerun in this order: board_structure_family H1/H2, range_arm_control (only one where a pass is plausible), sr_* family, premarket_depth_probe, then the rest. Recorded FAILs that also fail on absolute net stay FAIL.

### Gate warnings (pending evidence)

- X1: gate 'smoke' pending (trend-hold floor at the opening stop (floor_only))
- X2: gate 'smoke' pending (trend-hold off after a +15 bp peak (afterpeak_only))
- X3: gate 'smoke' pending (both trend-hold fixes (primary))
- X4: gate 'smoke' pending (trend hold off entirely (hold_off, reference))
- X5: gate 'smoke' pending (proportional peak lock 50% from +15 bp (lock50))
- X6: gate 'smoke' pending (idle step 0.01R instead of 0.05R (step01))
- X7: gate 'smoke' pending (park the stop at last-1c instead of selling at last (overtake_off))
- X8: gate 'smoke' pending (all Argon exit fixes together (argon_exits))
- X9: gate 'smoke' pending (tranche A: bank 40-60% at +15/+20/+25 bp, rest runs (4 arms))

## In flight

- **X1** [running] trend-hold floor at the opening stop (floor_only) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X2** [running] trend-hold off after a +15 bp peak (afterpeak_only) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X3** [running] both trend-hold fixes (primary) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X4** [running] trend hold off entirely (hold_off, reference) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X5** [running] proportional peak lock 50% from +15 bp (lock50) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X6** [running] idle step 0.01R instead of 0.05R (step01) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X7** [running] park the stop at last-1c instead of selling at last (overtake_off) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X8** [running] all Argon exit fixes together (argon_exits) - Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Read REPORT.md; skeptic review on any pass; operator decides.
- **X9** [queued] tranche A: bank 40-60% at +15/+20/+25 bp, rest runs (4 arms) - Replay fixed (tranche B breakeven). Optimistic fill model. Live gaps: rr clamp, market sell, 1R runner trail. Next: Smoke: day logs must show the new path firing (trail:initial_floor / after_peak / peak_lock / t1_hit) on the first 2 days; then read REPORT.md. Runs after X1-X8. A pass means build a live path, not flip a switch.
- **X10** [running] %R fast length 10 and half smoothing (rte_len, 2 arms) - About 9+ of 45 files done at 04:25; first run crashed and was relaunched. Next: Read REPORT.md when done.
- **F1** [collecting] forward: range position - Logging live on IEX, counts only until 150 entries. Next: Read about 11/13-11/17.
- **F2** [collecting] forward: run size (apex) - Logging live on IEX, counts only until 150 entries. Next: Read about 11/13-11/17.
- **F3** [collecting] forward: run efficiency - Logging live on IEX, counts only until 150 entries. Next: Read about 11/13-11/17.
- **F4** [collecting] forward: stall score - Logging live on IEX, counts only until 150 entries. Next: Read about 11/13-11/17.
- **F5** [collecting] forward: slope H1/H2 - Logging live on IEX, counts only until 150 entries. Next: Read about 11/13-11/17.

## Not started

- **N5** sector-ETF lead-lag (SMH, IBIT, IGV, XBI, URA) (Argon) - ETF bars fetched (12 ETFs, 120 days). No prereg yet. Next: Write prereg, skeptic review, build.
- **N6** H3 board breadth (simultaneous names turning up) (Argon) - Specified in the amended board_structure_family prereg; tool not built. Next: Build: 9/25-11/05 sessions, board from sources.jsonl.gz, SPY control.
- **N7** double arm (skip arm 1, buy arm 2 above its base) (operator/Argon) - Not specified or built. Next: Prereg first.
- **N16** queue ranking by freshness (pre_square before square, lowest rs_pos15) instead of max RVOL / move (Argon (logic audit 11/07)) - Config reading corrected: live rank_rvol_band is 0.5 (not 0.0) and move_band 2.0, so ties do get broken by _signal_rank; max positions 3 (not 2). Mechanism (extended names win the seat) is plausible, untested. Prior: gates on extension FAILED (room/overbought/stall). Next: Check what the rank actually picks vs what the book holds; prereg on the logged candidate sets.
- **N17** pre-square must also require slow %R rising (Argon (logic audit 11/07)) - Config corrected: ai_watch_presquare_after_square_min is already 10 (not off). Whether _presquare_exh_allows_buy ignores pctr_slow_rising is NOT yet verified in code. Prior: %R states are ~random and RSI/MACD add nothing. Next: Read the function; if true, replay variant with the extra condition.
- **X12** debounced sell trigger: breach by the damped print (or 2 prints) before flattening, hard stop excepted (Argon (logic audit 11/07)) - Code check: flatten_px is set from the raw minimum print (ai_positions.py:7585) while the shelf raises off a 2-print median (ring 2) - asymmetric as described. Direction of the bias is plausible; size untested. Prior: 'exit delay is loop starvation' and tight stops lose 11-24 bp. Next: Replay arm; needs a new knob for the debounce.
- **X13** smooth breakeven ramp (stop -15 bp at +15, 0 at +25, +15 at +35) instead of snapping to 0 at +15 (Argon (logic audit 11/07)) - Overlaps X5 (lock50) and X6; the dead zone itself is verified (X1-X8). The specific ramp is new. Next: Add as a trend_hold_fix-style arm after X1-X8 report, so one change is read at a time.
- **S4** scale initial stop, breakeven trigger and trail arm by vol_1m_pct (Argon (logic audit 11/07)) - ai_local_trail_give_vol_k is absent from live config (knob exists in code per Argon, unverified). All three thresholds are fixed bp today (-60/+15/+30). Data: vol_1m_pct <=0.07 = 47% never-green, >0.15 = 17%, net -9.8/-8.1/-7.4 bp (10/15+, n 445). Next: Counterfactual on outcomes: per-volatility-tercile stop/BE/arm, then a replay arm.
- **N18** same-wave re-entry reset: fast %R must cycle below -50 (or block re-entry after a never-green exit) (Argon (logic audit 11/07)) - Config corrected: reentry cooldown is 120 s (not 60) and max entries per symbol per day is 0 = unlimited (not 2). Prior: per-name attempt cap FAIL (4th/5th attempts were the best); loss cooldown unproven. A %R reset rule is different from a count cap. Next: Counterfactual on logged re-entries, then prereg.
- **X14** grace period after entry before the tight stop applies (entry clock 21-112 min vs exit leash 15-120 s) (Argon (logic audit 11/07)) - Config corrected: idle leash is 15 s (not 120 s). Prior: 42 stop/target/trail/ladder shapes all scored zero gross; wider stop LOST; 60% of cuts end better if held (payoff asymmetry). Next: Low prior; test last, as one arm of the sweep.

## By layer

### exit

| id | idea | source | status | evidence |
|---|---|---|---|---|
| X1 | trend-hold floor at the opening stop (floor_only) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X2 | trend-hold off after a +15 bp peak (afterpeak_only) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X3 | both trend-hold fixes (primary) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X4 | trend hold off entirely (hold_off, reference) | operator/Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X5 | proportional peak lock 50% from +15 bp (lock50) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X6 | idle step 0.01R instead of 0.05R (step01) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X7 | park the stop at last-1c instead of selling at last (overtake_off) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X8 | all Argon exit fixes together (argon_exits) | Argon | running | Replay restarted 05:29 on 503ef6ac after the audit fixes; the first run was discarded. |
| X9 | tranche A: bank 40-60% at +15/+20/+25 bp, rest runs (4 arms) | Argon | queued | Replay fixed (tranche B breakeven). Optimistic fill model. Live gaps: rr clamp, market sell, 1R runner trail. |
| X10 | %R fast length 10 and half smoothing (rte_len, 2 arms) | Gemini | running | About 9+ of 45 files done at 04:25; first run crashed and was relaunched. |
| X11 | triangle exit, leash 10/12, steep30 gate | operator | verdict_fail | Closed earlier (steep30 screen passed, confirm t 1.15). |
| S3 | payoff asymmetry fixes (hold winners longer / time-stop never-greens) | Argon | verdict_fail | Real asymmetry; both fixes lose (60% of cuts end better if held). |
| X12 | debounced sell trigger: breach by the damped print (or 2 prints) before flattening, hard stop excepted | Argon (logic audit 11/07) | idea | Code check: flatten_px is set from the raw minimum print (ai_positions.py:7585) while the shelf raises off a 2-print median (ring 2) - asymmetric as described. Direction of the bias is plausible; size untested. Prior: 'exit delay is loop starvation' and tight stops lose 11-24 bp. |
| X13 | smooth breakeven ramp (stop -15 bp at +15, 0 at +25, +15 at +35) instead of snapping to 0 at +15 | Argon (logic audit 11/07) | idea | Overlaps X5 (lock50) and X6; the dead zone itself is verified (X1-X8). The specific ramp is new. |
| X14 | grace period after entry before the tight stop applies (entry clock 21-112 min vs exit leash 15-120 s) | Argon (logic audit 11/07) | idea | Config corrected: idle leash is 15 s (not 120 s). Prior: 42 stop/target/trail/ladder shapes all scored zero gross; wider stop LOST; 60% of cuts end better if held (payoff asymmetry). |

### entry

| id | idea | source | status | evidence |
|---|---|---|---|---|
| N1 | V1 volume-clock %R | Argon | blocked | First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. |
| N2 | K absorption bar | Argon | underpowered | 101 triggers, needs 300. Not a verdict on the idea. |
| N3 | W opening washout | Argon | blocked | First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. |
| N4 | D dwell/knock | Argon | blocked | First run's same-name controls were biased (one-sided exclusion; controls +11..+28 bp vs -7.5 bp for random minutes). Fixed in 8a-fix commit and rerun started; the first FAILs are void. |
| N5 | sector-ETF lead-lag (SMH, IBIT, IGV, XBI, URA) | Argon | idea | ETF bars fetched (12 ETFs, 120 days). No prereg yet. |
| N6 | H3 board breadth (simultaneous names turning up) | Argon | idea | Specified in the amended board_structure_family prereg; tool not built. |
| N7 | double arm (skip arm 1, buy arm 2 above its base) | operator/Argon | idea | Not specified or built. |
| N8 | H1 peer lead-lag | Argon/Claude | verdict_fail | FAIL -9.4 bp, t -2.11 NOTE 11/07: controls reach t-30..t-16 (pre-trigger drift); the FAIL also rests on negative absolute net, but the lift numbers need a rerun with the P2 controls. |
| N9 | H2 stop-run reclaim | Argon/Claude | verdict_fail | FAIL -5.9 bp, lift 0 vs controls NOTE 11/07: controls reach t-30..t-16 (pre-trigger drift); the FAIL also rests on negative absolute net, but the lift numbers need a rerun with the P2 controls. |
| N10 | R5 regime follow-through gate | Argon/Claude | verdict_fail | FAIL +1.0 bp, t 1.16 |
| N11 | B2 beta decoupling | Argon/Claude | verdict_fail | FAIL: pure SPY exposure (+29 / -40 bp) |
| N12 | 1-2-3 pattern | Argon/Claude | verdict_fail | FAIL -13.7 bp/trade |
| N14 | relative strength scan | Argon/Claude | verdict_fail | FAIL -5.5 bp, t -0.53 |
| N13 | spread compression | Claude | underpowered | UNDERPOWERED and negative. |
| N15 | premarket rules on history | operator | void | 69% of alert names had too few premarket bars; R1 -281 bp/day. VOID, not a refutation. |
| N16 | queue ranking by freshness (pre_square before square, lowest rs_pos15) instead of max RVOL / move | Argon (logic audit 11/07) | idea | Config reading corrected: live rank_rvol_band is 0.5 (not 0.0) and move_band 2.0, so ties do get broken by _signal_rank; max positions 3 (not 2). Mechanism (extended names win the seat) is plausible, untested. Prior: gates on extension FAILED (room/overbought/stall). |
| N17 | pre-square must also require slow %R rising | Argon (logic audit 11/07) | idea | Config corrected: ai_watch_presquare_after_square_min is already 10 (not off). Whether _presquare_exh_allows_buy ignores pctr_slow_rising is NOT yet verified in code. Prior: %R states are ~random and RSI/MACD add nothing. |
| N18 | same-wave re-entry reset: fast %R must cycle below -50 (or block re-entry after a never-green exit) | Argon (logic audit 11/07) | idea | Config corrected: reentry cooldown is 120 s (not 60) and max entries per symbol per day is 0 = unlimited (not 2). Prior: per-name attempt cap FAIL (4th/5th attempts were the best); loss cooldown unproven. A %R reset rule is different from a count cap. |

### sizing

| id | idea | source | status | evidence |
|---|---|---|---|---|
| S1 | volatility-scaled sizing | Argon | verdict_fail | -7.4 vs -7.9 bp/trade |
| S2 | per-name daily attempt cap | Argon | verdict_fail | Later attempts not worse (4th -1.9, 5th 0.0 bp) |
| S4 | scale initial stop, breakeven trigger and trail arm by vol_1m_pct | Argon (logic audit 11/07) | idea | ai_local_trail_give_vol_k is absent from live config (knob exists in code per Argon, unverified). All three thresholds are fixed bp today (-60/+15/+30). Data: vol_1m_pct <=0.07 = 47% never-green, >0.15 = 17%, net -9.8/-8.1/-7.4 bp (10/15+, n 445). |

### live-code

| id | idea | source | status | evidence |
|---|---|---|---|---|
| L1 | stop built from planned entry, not the fill | Argon | open | Slippage since 10/26: mean +4.5 bp, median +2.3, 30% beyond 5 bp (n 1747) on a ~60 bp stop. |
| L2 | pre-entry prints can set the peak and arm breakeven | Argon | open | Plausible; '64 prints on tick 1' not confirmed in code. |
| L3 | live clamps ai_watch_synth_rr at max(0.25) | Argon | open | Verified: targets under +125 bp cannot be set live. |

### process

| id | idea | source | status | evidence |
|---|---|---|---|---|
| L4 | vacuity guard for sweeps | Claude | open | Replay arms can silently not run (X1/X3 floor was a no-op). |
| A1 | Argon 1R accounting point: R is 500 bp but the working stop is 60 bp | Argon (logic audit 11/07) | done | True (1R = 5% stop, working stop 0.12R) and already known (ratchet-must-hug-price-in-percent). The cited consequences are mostly moot: ai_dead_reentry_block is False and ai_reentry_min_mfe_r is 0.1R (not 0.5R). |
| P1 | audit control construction in every random-minute study tool; add control-sanity data gate | operator question 11/07 | open | bar_signal_family's one-sided exclusion inflated same-name controls (+11.6/+27.9 bp vs -7.5 bp for random minutes). board_structure_family (H1/H2) is symmetric (verified). ~20 other tools use random-minute controls and are unaudited: analyst_upgrade, book_walls, clock_windows, daily_filter_screen, early_entry_honest, first_presq_adversarial_argon, five_pillars_study, gate_counterfactual, momentum_alert_markouts, limit_below_study, momentum_runway_study, range_arm_control, runway_target_study, sector_study, sip_breakout_study and others. AUDIT RESULT (subagent + Argon, 11/07): none has the exact one-sided bug, but the stricter point holds - any same-name control drawn BEFORE the trigger sits on the path that made the stock fire. Flagged: sr_* family (5 tools, same-hour pool excluding only the trigger minute), range_arm_control (600 s exclusion vs 1800 s hold), premarket_depth_probe, sip_breakout_study, fib_pullback, structure_pullback, book_walls (300 s vs 600 s horizon), desk_vs_random/five_pillars/momentum_runway (full-day same-name), and board_structure_family H1/H2 (controls reach t-30..t-16). Clean: strictly-later or same-minute cross-name controls (early_entry_honest, limit_below, wr_trend_trade, etf_bars, first_presq_adversarial, analyst_upgrade, regime_beta_family B2 horizons). |
| P2 | control standard: same-name controls strictly AFTER the trade (t+hold+2 .. +62); plus a same-minute cross-name control; report control means beside the unconditional random-minute mean | early_entry_honest 10/13 calibration; Argon 11/07 | done | Implemented in bar_signal_family (rerun bsf_run3 in progress). Not yet applied to the older tools. |

### forward

| id | idea | source | status | evidence |
|---|---|---|---|---|
| F1 | forward: range position | Claude | collecting | Logging live on IEX, counts only until 150 entries. |
| F2 | forward: run size (apex) | Claude | collecting | Logging live on IEX, counts only until 150 entries. |
| F3 | forward: run efficiency | Claude | collecting | Logging live on IEX, counts only until 150 entries. |
| F4 | forward: stall score | Claude | collecting | Logging live on IEX, counts only until 150 entries. |
| F5 | forward: slope H1/H2 | Claude | collecting | Logging live on IEX, counts only until 150 entries. |

### data

| id | idea | source | status | evidence |
|---|---|---|---|---|
| D1 | ETF minute bars for thematic ETFs | Argon | done | 120 days, 12 ETFs in ai_reports/warehouse/etf. |
| D2 | SIP subscription | operator | parked | Operator: skip for now. Alpaca refunded the charge. |

