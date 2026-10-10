# Room below HOD out of period (2026-10-09): pre-registered PASS, ARTIFACT on review

Prereg: [room_hod_oos_prereg.json](room_hod_oos_prereg.json) (e19f14f). Tool: `tools/studies/room_hod_oos.py` (ccdd26c).
Report on the mini: `ai_reports/room_hod_oos/report.md`.

**Verdict: ARTIFACT. Room below the day's high is closed as a name filter for gappers.**

The script printed PRIMARY: PASS (pooled ROOM minus control +41.9 bp, t 4.61; halves +45.9 / +37.8). Checkpoint-2 review
(result-skeptic, 10/10) found the pass is produced by the pre-registered averaging order, which leaks hindsight:

- `needle_cells_oos.score_cell` averages each name-day's cell moments first, so every name-day gets equal weight however many
  cell moments it had. That count depends on the future path: a name that dips and recovers leaves few ROOM moments (its winners
  get full weight); a name that keeps falling spreads its weight thin. Mean gross per name-day by ROOM-moment count:
  <= 3: +225 bp (117), 4-10: +117 (144), 11-30: +75 (249), > 30: -7 (763).
- Non-anticipating weights: every moment equal, day-clustered **-1.6 bp (t -0.28)**, net -9.2; same-minute ROOM vs non-ROOM
  -7.6 (t -1.21); first ROOM moment per name-day -13.1 (t -0.93).
- Bid-ask bounce: not the cause (entering at bar i+1 close or i+2 open gives the same +40). No look-ahead in room_of/outcome/grid.
  SPY beta 2.53 vs 1.16 but not the cause.

## Data defect found in the same review

The sipbrk cache and the all-symbol daily panel are SPLIT-ADJUSTED, so price floors and bands were applied to adjusted prices.
72 name-days have adjusted/raw open ratios of 2-625 (reverse-split penny stocks; CPOP 6/10 raw $0.30 shown ~ $135). Rerun on raw
bars with a raw $20-100 band: same pattern (biased order +39.6, equal-weight +5.1 t 0.86, first moment -4.6).

Exposure: SIP_BREAKOUT_2026-10-03 and SR_BREAKOUT_HISTORY_2026-10-06 used this cache with price floors; their universes may
include reverse-split penny stocks. The needle C2 verdict (FAIL, -24.9 bp) used the same score_cell weighting; it failed in the
biased direction anyway, but should be re-scored with equal weight per moment before it is cited.

## Rules for any future cell test

Weight each decision equally (or score one decision per name-day chosen by a rule fixed in advance), give the control the same
weighting, apply price floors to RAW prices, and charge real per-name spreads.

## Addendum 2026-10-10: exposure re-scored

Script `tools/studies/rawfloor_rescore.py`; raw output [RAWFLOOR_RESCORE_2026-10-10_raw.md](RAWFLOOR_RESCORE_2026-10-10_raw.md).
In the 5/1-9/11 universe, 66 of 3,581 name-days opened below $10 raw (65 reverse splits, plus NOK 9/4 at $9.99).

- **SIP_BREAKOUT_2026-10-03:** 206 signals came from those name-days. Without them, capped net15 is -6.7 (t -1.7) and
  -5.9 (t -2.2), against -2.8 / -3.2 as published. **Still FAIL.** See that doc's addendum.
- **SR_BREAKOUT_HISTORY_2026-10-06:** already filtered on the raw open, so 0 scored events are affected. **Unchanged.**
- **Needle C2 (FAIL, -24.9 bp), re-scored.** Method: every C2 moment weighted equally; control = the same day-hour mean of
  other name-days' non-cell moments (as pre-registered); t clustered by day; raw $20-100 band; 10 bp cost. The raw
  band leaves the C2 population unchanged (134 name-days, 4,802 moments, 63 days).

| C2, raw $20-100 | half A | half B | pooled |
|---|---|---|---|
| net30 | -11.9 (t -2.1) | -10.3 (t -2.7) | **-11.0 (t -3.4)** |
| minus control | +0.0 (t +0.0) | +0.1 (t +0.0) | **+0.1 (t +0.0)** |

- Without the top 3 name-days the pooled control difference is -1.1 (t -0.4). The first C2 moment per name-day gives
  -4.6 (t -0.4). At 4 bp cost, net30 is -5.0.
- **The verdict stays FAIL,** because it requires net30 > 0 and at least +5 bp over control.
- **Cite C2 as "no different from other gappers at the same hour (~0 bp), net -11 bp after cost", not as -24.9.** Most
  of the -24.9 came from the same name-day-first weighting as the ROOM artifact, which worked against C2 here: the same
  order on the raw band gives -25.1.
- The needle line stays closed for gappers.
