# Pre-registration: −60 %R open-arm in the S/R gap (2026-10-05)

Machine-readable: [`sr_gap_wr60_arm_prereg.json`](sr_gap_wr60_arm_prereg.json).
**Committed before any −60 %R × support-gap outcome is computed on the sample.**

## Origin
Jonathan approved a NEW pre-registered test (2026-10-05): use the gap between support and
resistance and a **−60 %R open-arm gate** that fires closer to support, with a lot of room
until resistance. Sibling studies the same night (`order_block_gate`, `sr_range_trade`) both
**FAIL**; this is a different arm (≤ −60, not the live square at −20) gated by S/R location.

## Locked design (do not change after seeing results)

| Piece | Lock |
|---|---|
| Sample | Same SIP admitted name-days / sessions as `sr_range_trade_prereg.json` and `order_block_gate_prereg.json` (`ai_reports/indicator_levels/rows.json` + `ext.pkl`, $10+) |
| Halves | **Chronological** first half of sample days vs second half |
| Blocks | `tools/order_blocks.py`, point-in-time, charted last 3 per side, swing 10, wicks, 1-min |
| Arm | Fast %R (21, EMA 7) **≤ −60** at a 1-min close 09:40–15:30 ET; one per name per 15 min |
| Near support | Inside a charted support block, or within **0.30%** above its top |
| Room (primary) | ≥ **0.40%** to the bottom of the nearest charted resistance above; drop if inside resistance |
| Entry / exit | Next bar open → close 15 min later (15:55 cap) |
| Cost | 0.20% round trip ($10+ tier). Measured RT spread = mean(gross − net) of signal (= 20 bp under this model) |
| Control | Same-hour like-for-like: random minute (seed 31) in the same name-day and ET hour that also has %R ≤ −60 (fallback: same hour only, counted) |

## Pass bar (Jonathan)
In **both** chronological halves: signal mean **gross** − control mean **gross** **>** that half's
measured round-trip spread, with **≥ 50** signal trades per half. Also report net after costs.
Under 50 → **UNDERPOWERED**. Any **PASS** = **PENDING SKEPTIC REVIEW** (not a live gate).

## Information only
Room ≥ 0.60%; near-support 0.10% and inside-only; day-clustered t; win rate; median room /
distance-to-support; drop counts.

## Prior
Low. Order-block skip rule FAIL; support→resistance range trade FAIL vs like-for-like control;
10/02 near-support cells least-bad but negative.
