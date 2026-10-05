# Pre-registration: arm earlier on support instead of squares (2026-10-05)

Machine-readable: [`sr_support_arm_prereg.json`](sr_support_arm_prereg.json).
**Committed before any support-arm outcome is computed.** Queue item #4 in
[`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md).

| Piece | Lock |
|---|---|
| Names | Seated desk names: name-days with ≥ 1 E square arm (554 name-days, 16 days 09-11..10-02). 10/05 **excluded** (not in cached bars; no new fetch) |
| Halves | Chronological: A = 09-11..09-22 (8 days), B = 09-23..10-02 (8 days) |
| Blocks | `tools/order_blocks.py`, point-in-time, charted last 3 per side |
| Support arm | First bar per support block with low ≤ S.top and close ≥ S.btm (touch/reclaim), 09:40–15:30, not inside resistance, room ≥ **0.40%** to nearest resistance bottom (none above → no arm), 1 per name per 15 min. **No square, no %R required** |
| Square arm | E rows (desk square arms) on the same name-days |
| Outcome (both) | Next-bar open → close 15 min later (15:55 cap); net = gross − 0.20% |
| Primary | Matched name-days: support gross − square gross; day-clustered t |
| Measured RT | mean(gross − net) of support entries (= 20 bp) |

**Pass:** both halves, support − square gross > measured RT, ≥ 50 support arms per half. Report
net, n, t. Any PASS = **PENDING SKEPTIC REVIEW**.

**Minutes earlier:** first square time − first support time on matched name-days (median, mean,
share support-first); minutes since the last support arm before each square.

**Info only:** same-hour random-minute control (seed 37); room 0.25% / 0.60%; %R ≤ −50 required;
unmatched comparison.
