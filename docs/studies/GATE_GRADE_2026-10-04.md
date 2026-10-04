# Grading the gap-down and range-position gates on post-live refusals (2026-10-04)

> **Correction after the skeptic review (same day): the range-cap result is REJECTED; keep the cap at 90.**
> 1. **The cap delays names; it does not refuse them.** 303 of the 335 refused name-days were admitted later the same
>    day (298 at inclusion). The median delay was 1.3 minutes (mean 6.9). Comparing each name's 30-minute return
>    from its first refusal with the return from its actual later admission (next-bar open): +6.3 bp mean,
>    median 0.0, t 0.44. Removing the cap would buy almost nothing. The +107 bp compared *different names*
>    (those that ever touched the cap vs those that never did); it did not measure what the gate does.
> 2. **The entry leaked part of the move.** The price that set range_pos sat +37 bp (mean) above the last completed
>    1-minute close used as the entry. Entering at the next bar's open instead, the refused mean falls from
>    +47.8 to +6.8 bp, and every comparison fails the 2.24 bar (same-stage t 1.84; ≥ $10 t 1.76–1.87).
> 3. **It depends on which refusal you time from.** Refused name-days were refused a median of 4 times. Timed from
>    the last refusal, the diff is +24.6 bp, t 0.58.
> 4. Kept names under $10 (−313 bp) drive much of the gap. The same-stage check is fragile: the skeptic got kept n 87,
>    diff +134 vs this doc's 88 / +170; one name moves the kept mean by about 36 bp.
>
> **Net:** the cap is roughly inert. Its in-sample basis ("range position predicts the fade") is not supported out of
> sample, but removing it would change little. No config change. If it is ever re-graded, the honest test is a
> costed `replay_session.py --set ai_watch_admit_max_range_pos=0` over 9/16–10/2 with each day's own config.
> The tables below are kept as originally written, for the record.

Ideas list #11. Pre-registration: the docstring of `tools/studies/gate_grade.py`, committed before any outcome.
Post-hoc check (written after the first results, labelled as such): `tools/studies/gate_grade_inclusion.py`.
Data: `ai_reports/proposal_ledger/` on the mini, RTH decisions 09:45–15:00, free SIP 1-minute bars. Outcomes are
**gross** (no spread, no exits): close of the last completed 1-minute bar before the decision → 30 minutes later
(primary), and → 15:50 (information). t is day-clustered.

## Range-position cap (`ai_watch_admit_max_range_pos` 90; chosen on data to 9/5; ledger 9/16–10/2, 13 days)

| comparison | refused n / mean / median | let-through n / mean / median | refused minus let-through | t |
|---|---|---|---|---|
| **Pre-registered** (let-through = kept at any stage) | 335 / +47.8 / −3.0 | 824 / −59.3 / −18.6 | **+107.1 bp** | **+2.80** |
| ≥ $10 | 254 / +51.9 / 0.0 | 431 / −21.8 / −14.9 | +73.7 | +2.21 |
| to 15:50 (info) | | | +82.2 | +2.08 |
| *Post-hoc:* same stage and timing (let-through = kept at inclusion with a measured range_pos) | 335 / +47.8 / −3.0 | 88 / −122.3 / −25.7 | +170.1 | +2.88 |
| *Post-hoc:* 1% trimmed | | | +75.0 | +3.36 |

- Per day (30 min): positive on 11 of 13 days.
- 30-minute mean by range position at the gate: 0–50 −133 bp (n 74), 50–75 −57 (11), 75–90 −28 (7), 90–95 −28 (98),
  **95–100 +35 (220)**. The names at the very top of their range did best, which matches
  "buying the top is not the loss" (fills at the high did best, 871 fills).
- **Pre-registered reading (superseded, see the correction at the top): COSTS OPENS** (t 2.80 ≥ 2.24 Bonferroni). Out of sample, the gate removes names that went
  on to do better over the next 30 minutes than the names it let in. The in-sample basis
  ("range position predicts the fade") does not hold on the 13 later days.

**Not yet a config change.** The result is gross 30-minute drift from the 1-minute close, not a desk trade. The
refused medians are about 0, and the mean is carried by the right tail (the runners the ratchet is built to catch).
The let-through group at the same stage is small (88). Per the checkpoint rule this positive result goes to the
skeptic before anything ships. Any config change goes to the operator after a close, and would land in the cost-arm
config freeze (about 10 sessions from 10/1).

## Gap-down block (`ai_watch_gap_down_block_pct` 1.0; live 9/24; ledger 9/24–10/2, 7 days)

| comparison | refused n / mean | let-through n / mean | diff | t |
|---|---|---|---|---|
| Pre-registered, 30 min | 42 / −22.4 | 682 / −55.6 | +33.2 | +0.67 |
| to 15:50 (info) | 42 / +257.9 | 682 / −130.3 | +388.2 | +2.15 |
| Post-hoc same stage, 30 min | 42 / −22.4 | 381 / −34.9 | +12.5 | +0.22 |

- **Reading: UNCONFIRMED.** 42 refused name-days over 7 days is too few. For information only, the refused
  gap-downs rose to the close (+258 bp mean, +55 median), the opposite of the gate's premise that a gap-down fades all
  day. That premise is not supported out of sample, but it is not refuted either. Re-grade after about 10 more
  sessions.

## Caveats
- Gross returns from 1-minute closes. The desk pays about 7–15 bp of spread and exits on a trail, so a 30-minute mean
  is not trade P&L.
- 13 days (range) and 7 days (gap), June–October regime, one desk configuration per day (config churn).
- KEPT (pre-registered) mixes seed-stage and inclusion-stage names. The post-hoc same-stage check fixes the timing but
  shrinks the comparison group to 88.
