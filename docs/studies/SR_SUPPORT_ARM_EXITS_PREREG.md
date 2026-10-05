# Addendum pre-registration: support arm with no hold-window limits (2026-10-05)

Machine-readable: [`sr_support_arm_exits_prereg.json`](sr_support_arm_exits_prereg.json).
Parent: [`SR_SUPPORT_ARM_PREREG.md`](SR_SUPPORT_ARM_PREREG.md) (`3255be0`). **Committed before any
outcome of either prereg was computed.**

No 0.06R trail, no time decay, no overbought/triangle exits. Same entry (next-bar open) for the
support-touch arm, the square arm and the same-hour random-minute control.

| Exit | Rule |
|---|---|
| (a) | Hold to the 15:55 close |
| **(b) PRIMARY** | Exit when the high touches the bottom of the nearest point-in-time charted resistance above entry (fill max(T, open)); none above or never touched → 15:55 |
| (c) | Stop at support-block bottom × (1 − 0.1%) (support arm: the touched block; others: nearest support below entry, else no stop), checked first; target as (b); else 15:55 |
| (d) | Fixed 60 / 120 / 240 min (15:55 cap) |

**Pass (exit b):** both chronological halves, support − square gross on matched name-days > measured
RT (20 bp), ≥ 50 support arms per half. Report net, n, t, and the random control. Any PASS =
**PENDING SKEPTIC REVIEW**. The parent's 15-min primary keeps its own verdict.

**Info only:** exits (a)/(c)/(d); the gap arm and range trade S→R rerun with exits (a) and (b) vs
their own like-for-like controls (chronological 10/10 halves of their 20-day sample).
