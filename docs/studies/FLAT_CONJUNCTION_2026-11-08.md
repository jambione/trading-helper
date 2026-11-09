# Between-levels sideways, 2026-11-08: FAIL

Prereg `docs/studies/flat_conjunction_prereg.json`, written before this read. One score. Code `a97c18f7d4551f32` is the live `_range_shape_fields`, `_wr_slope_fields`, and `line_window`. Raw numbers: `docs/studies/flat_conjunction_score.json`.

Sideways means the series is traveling between a high and a low. All three live readings had to be true: 15-minute range at least 20 bp, path efficiency at or under 0.30, and net move at most half that range; the fast %R line traveled at least 15 points and gave back at least half; the slow line was not rising over 10 minutes (`wr_d10_slow <= 0`).

Window: 102 IEX sessions, 2026-04-29 through 2026-09-23, top 40 names by the prior session's dollar volume, price at least $20, grid 10:00–15:50 every 5 minutes. Outcome is the next 5 minutes, gross, before cost. 230,747 minutes. 9,126 were sideways (4,892 / 4,234 by half). Both halves had 51 contributing days. Powered.

| half | sideways n | sideways gross | kept gross | kept net at 4 bp | kept net at 8 bp | day t | day t, top 1% clipped |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 4,892 | −0.49 bp | +0.10 bp | −3.90 bp | −7.90 bp | 1.05 | 1.09 |
| B | 4,234 | −0.70 bp | −0.01 bp | −4.01 bp | −8.01 bp | 1.21 | 1.27 |

The skip points the same way in both halves, about +0.6 bp gross. The day-clustered t is under 1.96. After a 4 bp cost the kept minutes lose about 4 bp in both halves. The bootstrap 5th percentile of (kept mean minus all-minute mean) is −0.00 bp. Random same-count skip median is +0.00 bp; the rule's median is +0.02 bp. Never-green was 29.8% / 31.3% on sideways minutes and 32.2% / 35.2% on the rest, so these minutes are not the never-green pile.

Verdict: FAIL. Not a live gate. Thresholds stay where they were registered.

November watch entries, 2026-11-02 through 2026-11-06: 133 scored, 0 sideways. Rest gross versus the trade's own exit was −5.5 bp. INFORMATION only. Five sessions cannot pass or fail this rule.
