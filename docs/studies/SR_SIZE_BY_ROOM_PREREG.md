# Pre-registration: Size by room to resistance (2026-10-05)

Machine-readable: [`sr_size_by_room_prereg.json`](sr_size_by_room_prereg.json).
**Committed before any size-by-room outcome is computed on the sample.**

Queue: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) item **#2**
(after #1 exit-at-resistance **FAIL**).

## Origin
Jonathan: same opens; cut size when room to nearest resistance is under ~0.40%, full size
when wider.

## Locked design

| Piece | Lock |
|---|---|
| Sample | E square arms, same SIP indicator-levels sample (`rows.json` + `ext.pkl`, $10+) |
| Halves | Chronological first vs second half of sample days |
| Blocks | `tools/order_blocks.py`, point-in-time, charted last 3 per side |
| Room | % to nearest charted resistance bottom above price; none above => 0 |
| Floor (primary) | **0.40%** |
| Sized weights | **w = 0.5** if room < 0.40%, else **w = 1.0** |
| Control | Equal full size: w = 1 on every same open |
| Outcome | gross15 / net15 (15 min − 0.20%), contribution = w × return |
| Measured RT | mean(w) × 20 bp under the locked cost model |

## Pass bar (Jonathan)
In **both** chronological halves: mean(w×gross) − mean(gross) **>** that half's measured RT
(mean(w)×20 bp), with **≥ 50** arms per half. Also report net. Under 50 → **UNDERPOWERED**.
Any **PASS** = **PENDING SKEPTIC REVIEW**.

## Information only
Skip vs full (w∈{0,1}); floor 0.60% half-size; F fills; share under floor; day-clustered t.
