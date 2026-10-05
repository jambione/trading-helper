# Pre-registration: Exit-only at resistance (2026-10-05)

Machine-readable: [`sr_exit_resist_prereg.json`](sr_exit_resist_prereg.json).
**Committed before any exit-at-resistance outcome is computed on the sample.**

Queue context: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) (this is item #1).

## Origin
Jonathan approved an S/R test queue (2026-10-05), highest first: **exit-only at resistance**
(keep opens as-is; take profit at the bottom of the next resistance block; optional stop under
support). Entry/gate siblings the same day all FAIL; this isolates the exit.

## Locked design (do not change after seeing results)

| Piece | Lock |
|---|---|
| Sample | E square arms, same SIP indicator-levels sample as the order-block studies (`rows.json` + `ext.pkl`, $10+) |
| Halves | **Chronological** first vs second half of sample days |
| Blocks | `tools/order_blocks.py`, point-in-time, charted last 3 per side, swing 10, wicks, 1-min |
| Opens | **Kept as-is** — every E arm with a charted resistance bottom strictly above entry |
| Entry | Next bar open after the arm decision (as in the levels study) |
| Signal exit | TP at resistance bottom (high trades through → fill at target); time stop 30 min / 15:55; **no** support stop |
| Control | Same opens, same time stop only (no S/R) |
| Cost | 0.20% RT; measured RT = mean(gross − net) of signal (= 20 bp under this model) |

## Pass bar (Jonathan)
In **both** chronological halves: signal mean **gross** − control mean **gross** **>** that half's
measured round-trip spread, with **≥ 50** paired trades per half. Also report net after costs.
Under 50 → **UNDERPOWERED**. Any **PASS** = **PENDING SKEPTIC REVIEW** (not a live exit change).

## Information only
With support stop under nearest support; F fills; hit-target share; day-clustered t; drop counts.

## Caveats
SIP vs IEX; time stop ≠ live ratchet; limit fill at target assumed; target fixed at entry.
