# Pre-registration: support-touch arm + hold to 15:55, FORWARD test on live-desk sessions (2026-10-05)

Machine-readable: [`sr_support_hold_close_forward_prereg.json`](sr_support_hold_close_forward_prereg.json).
**Committed before any session on or after 2026-10-06 existed.** Observe-only / offline scoring: no config change,
no restart, no trading endpoints, no Databento. Queue item #5 in [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md).

**Motivation, not evidence:** in-sample (9/11–10/02, SIP) the support arm held to 15:55 beat a same-hour random
minute by +29.4 / +20.7 bp (t 3.7 / 3.3), but only as an *information* cell of a study whose registered primaries
FAILED ([`SR_SUPPORT_ARM_2026-10-05.md`](SR_SUPPORT_ARM_2026-10-05.md)). This forward test is the evidence.

| Piece | Lock |
|---|---|
| Sessions | Live desk sessions from **2026-10-06** on, with a usable recording. 10/05 and earlier are **excluded** (10/05 = parse dry-run + prior-day history only) |
| Universe | Names seated in the desk book (arm-poll rows in `decisions.jsonl.gz`), eligible from first appearance |
| Bars | 1-min OHLC from the desk's recorded prints (`prints.jsonl.gz`); recorded IEX 1-min bars (`wire.jsonl.gz`) fill gaps; ≥ 200 RTH minutes per name-day |
| Blocks | `tools/order_blocks.py`, point-in-time, charted last 3 per side |
| Arm | First touch/reclaim of a support block (low ≤ top, close ≥ bottom), 09:40–15:30, not inside resistance, room ≥ **0.40%**, 1 per name per 15 min, **no square / %R** |
| Entry / exit | Next 1-min bar open → **15:55 close** (primary) |
| Primary control | Same name-day, same ET hour random minute (seed 43), held to 15:55 |
| Info | Desk's own arms (`submitted` rows) held to 15:55; stop at block low − 0.1%; resistance-touch exit; desk SIP-spread input |
| Cost | Median recorded IEX quoted spread at support entries (quote ≤ 120 s old, 0–3%); charged **max(measured, 20 bp)** |
| Halves | Chronological over scored sessions: A = first ⌊n/2⌋, B = rest |

**Stopping (no peeking at outcomes):** score once at the first close with ≥ 10 held-out sessions **and** ≥ 100
support arms per half; until then only counts may be checked. Hard stop at 20 sessions (~11/02); under 100 arms in a
half then = UNDERPOWERED. **Earliest scoring: after the 2026-10-19 close.**

**Pass:** both halves, mean(support − same-hour random) gross at 15:55 > charged cost. Report n, gross, net, lift,
day-clustered t. Any PASS = **PENDING SKEPTIC REVIEW**; it would not be a live arm without a separate approval.
