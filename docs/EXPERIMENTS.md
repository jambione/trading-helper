# Experiment board (generated from docs/experiments/registry.json, 2026-11-07)

Pipeline: idea -> prereg -> skeptic -> tool -> smoke -> data_check -> running -> verdict. Edit the registry, not this file; run `tools/experiments.py board`.

## Counts

| status | n |
|---|---|
| idea | 3 |
| queued | 1 |
| running | 9 |
| collecting | 5 |
| blocked | 3 |
| open | 4 |
| verdict_fail | 10 |
| underpowered | 2 |
| void | 1 |
| done | 1 |
| parked | 1 |

## Needs attention

- **N1** [blocked] V1 volume-clock %R - Run says FAIL (-4.5 bp, lifts very negative) but the random-minute controls average ~+9 bp net, which is implausible. Next: Diagnose the control calculation, rerun if it is a bug. (blocked on: control sanity check)
- **N3** [blocked] W opening washout - Run says FAIL (-12.3 bp) but shares the same control code as N1. Next: Same control sanity check. (blocked on: control sanity check)
- **N4** [blocked] D dwell/knock - Run says FAIL (-6.1 bp, lift vs first-try breakout -3.3) but shares the control code. Next: Same control sanity check. (blocked on: control sanity check)
- **L1** [open] stop built from planned entry, not the fill - Slippage since 10/26: mean +4.5 bp, median +2.3, 30% beyond 5 bp (n 1747) on a ~60 bp stop. Next: Candidate live change; needs the operator's say.
- **L2** [open] pre-entry prints can set the peak and arm breakeven - Plausible; '64 prints on tick 1' not confirmed in code. Next: Check against logged peaks before touching anything.
- **L3** [open] live clamps ai_watch_synth_rr at max(0.25) - Verified: targets under +125 bp cannot be set live. Next: Needed only if X9 passes.
- **L4** [open] vacuity guard for sweeps - Replay arms can silently not run (X1/X3 floor was a no-op). Next: Add one-day smoke check to sweep_replays.py.

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

### entry

| id | idea | source | status | evidence |
|---|---|---|---|---|
| N1 | V1 volume-clock %R | Argon | blocked | Run says FAIL (-4.5 bp, lifts very negative) but the random-minute controls average ~+9 bp net, which is implausible. |
| N2 | K absorption bar | Argon | underpowered | 101 triggers, needs 300. Not a verdict on the idea. |
| N3 | W opening washout | Argon | blocked | Run says FAIL (-12.3 bp) but shares the same control code as N1. |
| N4 | D dwell/knock | Argon | blocked | Run says FAIL (-6.1 bp, lift vs first-try breakout -3.3) but shares the control code. |
| N5 | sector-ETF lead-lag (SMH, IBIT, IGV, XBI, URA) | Argon | idea | ETF bars fetched (12 ETFs, 120 days). No prereg yet. |
| N6 | H3 board breadth (simultaneous names turning up) | Argon | idea | Specified in the amended board_structure_family prereg; tool not built. |
| N7 | double arm (skip arm 1, buy arm 2 above its base) | operator/Argon | idea | Not specified or built. |
| N8 | H1 peer lead-lag | Argon/Claude | verdict_fail | FAIL -9.4 bp, t -2.11 |
| N9 | H2 stop-run reclaim | Argon/Claude | verdict_fail | FAIL -5.9 bp, lift 0 vs controls |
| N10 | R5 regime follow-through gate | Argon/Claude | verdict_fail | FAIL +1.0 bp, t 1.16 |
| N11 | B2 beta decoupling | Argon/Claude | verdict_fail | FAIL: pure SPY exposure (+29 / -40 bp) |
| N12 | 1-2-3 pattern | Argon/Claude | verdict_fail | FAIL -13.7 bp/trade |
| N14 | relative strength scan | Argon/Claude | verdict_fail | FAIL -5.5 bp, t -0.53 |
| N13 | spread compression | Claude | underpowered | UNDERPOWERED and negative. |
| N15 | premarket rules on history | operator | void | 69% of alert names had too few premarket bars; R1 -281 bp/day. VOID, not a refutation. |

### sizing

| id | idea | source | status | evidence |
|---|---|---|---|---|
| S1 | volatility-scaled sizing | Argon | verdict_fail | -7.4 vs -7.9 bp/trade |
| S2 | per-name daily attempt cap | Argon | verdict_fail | Later attempts not worse (4th -1.9, 5th 0.0 bp) |

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

