# Exit/opens sweep 2026-10-16 (prereg exit_cut_sweep amended_3, sha 8bbede5, 8 days 10/06-10/15, data-account daytime run)

**trail_dc (live since 09:32 10/16) KEPT:** vs off +40.8 bp/day, t 2.29 (8 days); half A +56.9 (t 3.31), half B +24.8 (t 0.78) - beats off in both halves with paired t >= 1.5. Win 45% -> 56%, hold 151 -> 214 s, opens 40.2 -> 37.0/day.

**No new arm qualifies vs trail_dc (need both halves and t >= 1.5):** cool30 +7.2 (t 0.31; halves -37.4 / +51.7 disagree; opens +4/day); lob (engine-only triangle) +2.7 (t 0.19); init06 (0.3% initial stop) +0.9 (t 0.03); seat150 -17.3 (t -1.04; +3 opens/day but worse value); np_dc -22.3 (t -0.6). Live config unchanged. sqwait0/sqwait5 not yet run.

days found 8; scored 8 ['2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15']; incomplete []; vacuous []

| arm | opens/day | opens/10m | concurrency | win % | median hold s | net bp/trade (t) | value bp/day (t) | no NBBO |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| off | 40.2 | 1.10 | 0.70 | 45% | 151 | -5.0 (-2.47) | -159.3 (-2.31) | 0 |
| trail_dc | 37.0 | 1.01 | 0.69 | 56% | 214 | -3.8 (-1.61) | -118.4 (-1.61) | 0 |
| trail_dc_seat150 | 40.2 | 1.10 | 0.74 | 57% | 197 | -3.9 (-2.01) | -135.7 (-2.06) | 0 |
| trail_dc_cool30 | 41.1 | 1.13 | 0.78 | 56% | 194 | -3.4 (-1.62) | -111.3 (-1.68) | 0 |
| trail_dc_lob | 37.2 | 1.02 | 0.65 | 55% | 200 | -3.5 (-1.77) | -115.8 (-1.73) | 0 |
| trail_dc_init06 | 39.1 | 1.07 | 0.61 | 51% | 162 | -3.4 (-1.61) | -117.6 (-1.76) | 0 |
| trail_dc_np_dc | 40.9 | 1.12 | 0.47 | 50% | 180 | -4.0 (-2.04) | -140.8 (-2.10) | 0 |

Paired per-day VALUE differences vs the references (t >= 2 needed to call one better):
  trail_dc - off: +40.8 bp/day (t 2.29, 8 days)
  trail_dc_seat150 - off: +23.6 bp/day (t 0.91, 8 days)
  trail_dc_cool30 - off: +48.0 bp/day (t 2.13, 8 days)
  trail_dc_lob - off: +43.5 bp/day (t 2.44, 8 days)
  trail_dc_init06 - off: +41.7 bp/day (t 1.02, 8 days)
  trail_dc_np_dc - off: +18.5 bp/day (t 0.45, 8 days)
  off - trail_dc: -40.8 bp/day (t -2.29, 8 days)
  trail_dc_seat150 - trail_dc: -17.3 bp/day (t -1.04, 8 days)
  trail_dc_cool30 - trail_dc: +7.2 bp/day (t 0.31, 8 days)
  trail_dc_lob - trail_dc: +2.7 bp/day (t 0.19, 8 days)
  trail_dc_init06 - trail_dc: +0.9 bp/day (t 0.03, 8 days)
  trail_dc_np_dc - trail_dc: -22.3 bp/day (t -0.6, 8 days)
