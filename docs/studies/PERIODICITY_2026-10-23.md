# Same half-hour as yesterday (intraday periodicity): FAIL (2026-10-23)

- **Prereg:** `docs/studies/periodicity_prereg.json` (as amended, c3264bb5).
- **Code:** `tools/studies/periodicity.py`, sha 0f2a8aa6. Skeptic code review: READY after round 2.
- **Data:** the warehouse, ~1,640 liquid names a day, 110 sessions.
- **Design:** Heston, Korajczyk & Sadka (2010). For each half-hour slot (10:00-16:00), buy the top decile of names by their average return in that same slot over the last 20 sessions. Hold the slot (30 min). LIFT = the top decile minus the average name.

## Result

| | Half A | Half B |
|---|---|---|
| Slot-trades | 108,434 | 107,789 |
| LIFT | +1.6 bp (t 1.38) | +1.0 bp (t 1.74) |
| 97.5% upper bound | 3.8 bp | 2.0 bp |
| Top decile net of 5 bp | −4.4 bp (t −2.6) | −4.2 bp (t −4.7) |

**Verdict: FAIL.** The upper bound of LIFT is below the +5 bp bar in both halves.

**Reading.** The published effect is there and points the right way (+1 to +1.6 bp per slot, in both halves). It is far too small to pay the spread. This is the "real but small" pattern again, like order flow.

## Information
- **The last half hour (15:30-16:00):** +3.3 / +4.0 bp. It is the strongest slot, as the paper says, but still under the 5 bp cost.
- **The first half hour:** +0.5 / +12.7 bp. The halves disagree.
- **Other cuts:**
  - bottom decile: −2.5 / −0.3 bp;
  - $20+ names: +1.3 / +0.9 bp;
  - 5-session look-back: +2.0 / +0.8 bp;
  - 40-session look-back: −1.0 / +0.9 bp. This look-back covers only the names that already have the 20-session signal, and it is incomplete before 06-22.

## Declared
- About 13% of name-slots lack the exact first-minute bar and are skipped before entry. The skip applies equally to the top decile and to the benchmark.
- The placebo MDE (0.5 bp) understates the noise, so power was read from the upper bound.

**No desk change.**
