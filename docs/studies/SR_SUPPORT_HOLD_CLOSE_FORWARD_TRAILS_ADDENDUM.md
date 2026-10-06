# Addendum to the forward test (prereg `c1e9c30`, queue #5): trailing-stop exits, INFORMATION ONLY

Machine-readable: [`sr_support_hold_close_forward_trails_addendum.json`](sr_support_hold_close_forward_trails_addendum.json).
**Registered 2026-10-06 ~03:57 ET, before the 10/06 open; no 10/06 session data exists yet.** Jonathan approved.
Parent prereg: [`SR_SUPPORT_HOLD_CLOSE_FORWARD_PREREG.md`](SR_SUPPORT_HOLD_CLOSE_FORWARD_PREREG.md).

**Why added (not evidence):** added *after* the in-sample worked example (9/11–10/02 SIP), which showed the
hold-to-15:55 average comes entirely from the top 10% of trades, and after Jonathan asked how profit gets secured.

**These rows cannot change the verdict.** Unchanged from `c1e9c30`: primary exit (15:55 close), primary control
(same-hour random minute, seed 43), desk-arm info control, cost (max(measured IEX round trip, 20 bp)), halves,
minimum sample / stopping rule, pass bar. Trail rows print only inside the same gated score run; the dry run
stays counts only.

| Variant | Rule (else 15:55 close) |
|---|---|
| T1 | Stop 1.0% below the running high since entry (starts at entry × 0.99) |
| T2 | Stop 2.0% below the running high since entry (starts at entry × 0.98) |
| T3 | No stop until up +1.0% (running high ≥ entry × 1.01); then stop = max(entry, running high × 0.985) |
| T4 | Stop at support-block bottom × 0.999; once up +1.0%, stop = max(that, running high × 0.985). Square/random use the highest charted support block below entry known at the decision; none → trail part only |

**Mechanics:** entry = next bar open (unchanged). Bar j's stop uses the running high through bar j−1 (start = entry),
so a bar never moves its own stop. low ≤ stop → exit at min(stop, open) (gap-through fills at the open). Applied
identically to the support arm, desk/square arm and random control, on the desk's recorded IEX-based 1-min bars.

**Reported (info only, per half):** n, gross, support − random (same variant, day-clustered t), net (− charged
cost), median, % winners, give-back vs hold = mean(variant − 15:55) per trade.

**In-sample (optional):** the same 4 variants on the 9/11–10/02 SIP cache (cost 20 bp), labelled IN-SAMPLE / INFO
ONLY / already mined. Widths 1.0/2.0/1.5%, activation +1.0%, block offset −0.1% are fixed; anything else is a new
pre-registration.
