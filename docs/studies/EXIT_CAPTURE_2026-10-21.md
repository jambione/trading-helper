# Exit capture: the exits keep a fast edge; the entries are the problem (2026-10-21)

Prereg: [exit_capture_prereg.json](exit_capture_prereg.json) (amended before any run). Script:
`tools/studies/exit_capture.py` at 780c412f, run on the mini against the live code (2229d1c1) and the live config
(sha cd84f710). Raw: [EXIT_CAPTURE_2026-10-21_raw.md](EXIT_CAPTURE_2026-10-21_raw.md).

**The question (operator 10/21).** Are the exits excellent and only waiting for better entries, or do they leak an edge?
Method: inject a known drift into the real post-entry SIP paths of the desk's 904 paper fills (851 replayable in every
cell) and run the live exit code unchanged. That code includes the trail trend hold and the live dead_trade rule.

**Fidelity passed.** On the 123 fills traded under today's config, the zero-drift replay exits within 60 s of live for
63% of them (bar 55%). It nets -10.1 bp against live's -6.3 bp (gap 3.8 bp, bar 5).

**Verdict: KEEPS AN EDGE** (pre-registered bar: capture >= 0.70 at +10 and +20 bp over 3 minutes, CI above 0.5).

| Injected entry edge | Arrives over | Net per trade | Exits keep | Median hold |
|---|---|---|---|---|
| 0 (real) | - | -8.3 bp | - | 192 s |
| +10 bp | 3 min | -0.5 bp | 78% | 152 s |
| +20 bp | 3 min | +5.9 bp | 71% | 138 s |
| +40 bp | 3 min | +19.3 bp | 69% | 133 s |
| +10 / +20 bp | 10 min | -4.0 / -1.1 bp | 43% / 36% | 182 / 161 s |
| +10 / +20 bp | a jump at 60 s | +1.3 / +9.5 bp | 96% / 89% | 123 / 94 s |
| -10 bp (a bad entry) | 3 min | -16.3 bp | 79% of the loss | 383 s |

**What it means.**

- **The exits are good at a fast run.** They keep 70-78% of an edge that arrives within 3 minutes, and about 90-96% of
  a quick jump. They do not destroy an entry edge.
- **They do not stretch for a run.** The naive capture (exit times unchanged from the real paths) is 0.75 at 3 minutes,
  about what the exits achieve. Holds get shorter as the drift grows (192 s to 133 s), because the ratchet tightens as
  price rises. Against a slower 10-minute run they keep only 36-43%. A plain hold beats them by 5.5-12.4 bp (t -10 to
  -21), and it also beats them on 3-minute runs by 2-6 bp. The exits beat a hold only when the entry is bad: +2.0 bp at
  3 minutes and +4.7 bp at 10 minutes on a -10 bp entry.
- **Cheap names leak.** At 3 minutes the exits keep 54% / 32% under $20, against 80-88% above $20.
- **The entry bar.** The desk breaks even only if entries carry about +11 bp of 3-minute edge (95% CI 7-15 bp; about +5
  bp after shifting to live's slightly better booked results). For a 10-minute run the bar is about +24 bp (CI 16-33). Today's
  entry studies measured about 0 at 3 and 10 minutes.

**Answer to the operator.** The exits are not throwing away an edge: for scalp-speed runs they keep most of one. The
system loses because the entries carry none. A +5 to +11 bp 3-minute entry edge would turn the current desk profitable
with the exits as they are. If a future entry's edge is slower (runs over 10 minutes), the exits as tuned would leak half of it,
and a hold-longer mode would be the fix. Cheap names are the weak spot either way.

Caveats: the injected drift is smooth; the overbought exit and T1 scale-out are off live and not modelled; triggers use
SIP prints (live uses IEX); the stale-data flatten is not modelled; mu = 5 cells are reported but not judged (cent
rounding).
