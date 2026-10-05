# Pre-registration: Breakout confirmation — not the first poke (2026-10-05)

Machine-readable: [`sr_breakout_confirm_prereg.json`](sr_breakout_confirm_prereg.json).
**Committed before any breakout-confirmation outcome is computed.**

Queue: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) item **#2b**.

## Origin
Jonathan: require a close above resistance plus a higher low (or volume) before treating a
breakout as a run — not the first poke.

## Locked design

| Piece | Lock |
|---|---|
| Sample | Same SIP admitted name-days as sibling order-block studies |
| Halves | Chronological |
| Break | Charted bearish OB becomes breaker (order_blocks.py); first break per block per name-day; 09:40–15:00 |
| Control | First poke: buy next open after break bar |
| Signal | Confirm within 15 min: close > broken top **and** higher low vs break bar; buy next open |
| Primary | Paired events with both poke and confirmed entries; lift = confirmed − poke gross |
| Cost / RT | 0.20%; measured RT = 20 bp |

## Pass bar
Both chronological halves: mean paired lift **>** measured RT (20 bp), **≥ 50** pairs.
Report net. **PASS** = **PENDING SKEPTIC REVIEW**.

## Information
Volume confirmation; higher-low OR volume; unpaired; poke-only (never confirm).
