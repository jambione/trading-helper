# Lower half of the between-levels range, 2026-11-08: FAIL

Prereg `docs/studies/range_bisect_prereg.json`, written before any split by position. One score. Code `a97c18f7d4551f32`, midpoint 0.5. Raw numbers: `docs/studies/range_bisect_score.json`.

An entry had to be sideways under the frozen between-levels rule, and the live price had to sit at or below the midpoint of that 15-minute high and low (`rs_pos15 <= 0.5`). The upper half of the same minutes is the comparison. The parent skip-everything read on this window had already failed. Its pooled means were not used as a target.

Window: 102 IEX sessions, 2026-04-29 through 2026-09-23. Same grid as the parent. 230,747 minutes. 6,484 lower half (3,516 / 2,968) and 2,642 upper half (1,376 / 1,266). Both halves had 51 contributing days. Powered.

| half | lower gross | upper gross | lower minus upper | lower net at 4 bp | day t | day t, top 1% clipped |
|---|---:|---:|---:|---:|---:|---:|
| A | −0.77 bp | +0.21 bp | −0.99 bp | −4.77 bp | −0.66 | −0.66 |
| B | −0.75 bp | −0.58 bp | −0.17 bp | −4.75 bp | +0.22 | +0.19 |

The lower half is the worse half on the pooled minutes in both halves. The equal-day t is negative in A and about zero in B. Neither side clears 1.96. The lower half also sits below the unconditional mean (+0.08 bp / −0.03 bp). After 4 bp the lower-half book loses about 4.8 bp in both halves. The bootstrap median of (lower-half mean minus all-minute mean) is −0.82 bp. A random skip of the same count is about 0. Never-green was 30.2% / 31.2% on the lower half and 28.9% / 31.6% on the upper half.

Verdict: FAIL. Not a live gate. The midpoint stays at 0.5. The sideways thresholds stay where they were.

November watch entries, 2026-11-02 through 2026-11-06: 133 scored, 0 in either half. INFORMATION only.
