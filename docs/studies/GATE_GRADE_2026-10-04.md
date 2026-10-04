# Grading the gap-down and range-position gates on post-live refusals (2026-10-04)

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
- **Pre-registered reading: COSTS OPENS** (t 2.80 ≥ 2.24 Bonferroni). Out of sample, the gate removes names that went
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
