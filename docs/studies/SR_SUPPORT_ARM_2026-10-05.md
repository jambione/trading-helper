# Arm earlier on support instead of squares (+ exits without desk hold limits) — result (2026-10-05)

Pre-registrations, both committed and pushed **before any outcome was computed**:
- [`sr_support_arm_prereg.json`](sr_support_arm_prereg.json) / [`SR_SUPPORT_ARM_PREREG.md`](SR_SUPPORT_ARM_PREREG.md):
  **`3255be00b47b72195d9cd707e889f6767c14d6a1`** (support-touch arm vs square arm, 15-min hold)
- Addendum [`sr_support_arm_exits_prereg.json`](sr_support_arm_exits_prereg.json) /
  [`SR_SUPPORT_ARM_EXITS_PREREG.md`](SR_SUPPORT_ARM_EXITS_PREREG.md): **`afc50c0398d69d3b4077ee67e36e7a49982d6795`**
  (exit families; **(b) resistance touch is the only deciding exit**)

Script `tools/studies/sr_support_arm.py`. Full printout: [`sr_support_arm_2026-10-05_raw.md`](sr_support_arm_2026-10-05_raw.md);
rows in `ai_reports/sr_support_arm/`. Queue item #4 in [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md).

Sample: 554 seated name-days (name-days with ≥ 1 desk square arm), 16 days 09-11..10-02, SIP 1-min bars.
**10/05 is not in the cached bars and was excluded** (no new fetch). Chronological halves: A 09-11..09-22,
B 09-23..10-02. Support arm = first touch/reclaim of a charted support block (low ≤ top, close ≥ bottom),
room ≥ 0.40% to the nearest charted resistance, not inside resistance, no square or %R needed.
Cost 0.20% round trip, so the measured RT spread is 20 bp.

## Verdicts

| Primary | Half A (n support 342 / square 430) | Half B (n support 823 / square 913) | Verdict |
|---|---|---|---|
| **Parent: 15-min hold** | support − square **−10.2 bp** (t −0.40); net −18.7 vs −8.5 | **+5.0 bp** (t +0.80); net −3.2 vs −8.3 | **FAIL** |
| **Addendum: exit (b) resistance touch** | support − square **−22.5 bp** (t −0.41); net −28.3 vs −5.7 | **+23.2 bp** (t +0.93); net +9.4 vs −13.8 | **FAIL** |

The bar is beat by more than 20 bp in both halves. Exit (b) clears 20 bp in half B only (t 0.93), and half A goes
the other way. Neither primary passes, so there is nothing to send to skeptic review.

## Minutes earlier

- On the 438 matched name-days, the first support arm comes a **median 32 min (mean 24 min) before** the first
  square. Support fires first on 59% of name-days and the square first on 40%.
- 53% of square arms (957 / 1,809) had an earlier support arm on the same name-day. The median lead was 69 min
  (IQR 29–128).

## Per-half table, all exit families (gross / net bp per trade; t is day-clustered)

Families other than (b) are **information only**.

| exit | half | support n | support gross / net (t) | square n | square gross / net | support − square (t) | support − same-hour random (t) |
|---|---|---|---|---|---|---|---|
| 15 min | A | 342 | +1.3 / −18.7 (−1.08) | 430 | +11.5 / −8.5 | −10.2 (−0.40) | +6.4 (+0.39) |
| 15 min | B | 823 | +16.8 / −3.2 (−0.95) | 913 | +11.7 / −8.3 | +5.0 (+0.80) | +13.0 (+2.35) |
| (a) 15:55 | A | 342 | +80.9 / +60.9 (+0.59) | 430 | +19.0 / −1.0 | +61.9 (+1.44) | +29.4 (+3.69) |
| (a) 15:55 | B | 823 | +61.4 / +41.4 (+1.35) | 913 | +15.3 / −4.7 | +46.1 (+1.79) | +20.7 (+3.25) |
| **(b) resist** | A | 342 | −8.3 / −28.3 (−0.55) | 430 | +14.3 / −5.7 | **−22.5 (−0.41)** | +0.6 (+0.02) |
| **(b) resist** | B | 823 | +29.4 / +9.4 (+0.53) | 913 | +6.2 / −13.8 | **+23.2 (+0.93)** | +17.9 (+2.66) |
| (c) stop+tgt | A | 342 | +14.1 / −5.9 (−1.03) | 430 | +11.0 / −9.0 | +3.1 (+0.08) | −3.6 (−0.31) |
| (c) stop+tgt | B | 823 | +12.4 / −7.6 (−1.14) | 913 | −3.6 / −23.6 | +16.1 (+0.80) | +14.7 (+4.87) |
| (d) 60 min | A | 342 | +50.9 / +30.9 (+0.52) | 430 | +26.6 / +6.6 | +24.3 (+0.63) | +70.7 (+1.98) |
| (d) 60 min | B | 823 | +34.2 / +14.2 (+1.56) | 913 | +6.0 / −14.0 | +28.2 (+2.34) | +18.0 (+2.24) |
| (d) 120 min | A | 342 | +48.9 / +28.9 (+0.35) | 430 | +22.8 / +2.8 | +26.1 (+0.44) | +23.2 (+2.52) |
| (d) 120 min | B | 823 | +52.3 / +32.3 (+1.87) | 913 | +12.5 / −7.5 | +39.8 (+1.72) | +30.8 (+3.11) |
| (d) 240 min | A | 342 | +76.0 / +56.0 (+0.56) | 430 | +32.7 / +12.7 | +43.3 (+0.85) | +28.4 (+2.73) |
| (d) 240 min | B | 823 | +60.9 / +40.9 (+1.63) | 913 | +18.7 / −1.3 | +42.2 (+1.95) | +17.6 (+2.43) |

### How to read the long-hold rows (information only, not a pass)

- In both halves, support − square exceeds 20 bp under **(a) hold to 15:55**, (d) 60, 120 and 240 min.
  These were pre-declared as information, not deciding exits.
- Six families with two comparisons each were run. Day-clustered t on support − square is weak in half A
  (0.44–1.44), with only 8 day clusters per half.
- Long holds on these 16 days carry a large up-drift: support-arm net is positive, but its day-clustered t is
  below 2 in every cell.
- The desk's square arms look **worse than random minutes** at the same hour on long holds (square's random
  control: (a) +97.8 / +60.3 bp vs squares +19.0 / +15.3). So "support beats squares" partly means "squares are a
  bad moment", not that support is a good one.
- Against its own same-hour random control, the support arm is ahead under (a), with t +3.69 / +3.25.
- **This is a hypothesis for a NEW pre-registration on held-out sessions** (IEX, 10/06 onward): support-touch arm
  plus hold-to-close, against the same-hour random control. It must not be read as a result here.

## Info: room and %R variants (exits 15 min and b, against all square arms)

- Room ≥ 0.25%: exit b A −51.1 (t −1.71), B +18.7 (t +1.03).
- Room ≥ 0.60%: exit b A −54.0, B +21.7.
- Room ≥ 0.40% plus %R ≤ −50: exit b A −38.6, B +19.6.
- 15-min rows are all within ±13 bp.
- No variant clears 20 bp in both halves.

## Info: earlier S/R ideas rerun without desk hold limits (own samples, chronological 10/10 halves)

| idea | exit | half A: n, gross / net, vs control (t) | half B: n, gross / net, vs control (t) |
|---|---|---|---|
| gap arm (−60 %R near support) | (a) 15:55 | 355, +44.3 / +24.3, **+19.3 (t +9.67)** | 1800, +38.0 / +18.0, +5.5 (t +2.54) |
| gap arm | (b) resist | 355, +11.7 / −8.3, +23.5 (t +0.79) | 1800, +14.9 / −5.1, +7.0 (t +1.71) |
| range trade S→R | (a) 15:55 | 77, −39.4 / −59.4, −21.4 (t −1.50) | 408, +118.1 / +98.1, +11.7 (t +1.55) |
| range trade S→R | (b) resist | 77, −59.1 / −79.1, −46.0 (t −1.22) | 408, +24.7 / +4.7, +8.7 (t +0.85) |

None of these lifts exceeds 20 bp in both halves. The gap arm's half-A t of 9.67 comes from 5 signal days, so the
clustered t is not trustworthy.

## Caveats

- SIP bars, while the live desk uses IEX.
- Flat 0.20% cost regardless of hold length.
- Limit fill at resistance is assumed on a touch.
- Targets and stops are fixed at the decision bar.
- 10/05 is excluded.
- The same 16–20 sessions have now been mined by eight order-block studies.
- No config change: `ai_watch_ob_resist_skip=true` is untouched.
