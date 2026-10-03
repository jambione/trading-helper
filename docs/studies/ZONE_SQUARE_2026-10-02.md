# Zone + square: squares after a dip into a zone below the book-entry price (2026-10-02)

> **Skeptic review 2026-10-03** ([SKEPTIC_REVIEW_2026-10-02.md](SKEPTIC_REVIEW_2026-10-02.md) §7): squares fail — confirmed.
> **The ZONE_ONLY rows below overstate the loss**: they book every fill at the zone price even when the bar opened below it,
> and count names already through the zone before 09:40. The corrected arm (ZONE_FIX, post-review, not pre-registered) is
> in the section at the end; "the zone rule is the worst entry measured" does not hold.

Pre-registered: `zone_square_prereg.json` (d43fcd3). Script: `tools/studies/zone_square_study.py`.
608 admitted name-days at $10+ with a book entry (9/04–10/02), 1,212 squares after the entry, 29,958 control minutes.
Outcome: 15 minutes after entry minus 0.20% round trip. Lift = vs random minutes in the same name-day and hour, after the book entry.

| arm | A lift bp (t, n) | B lift bp (t, n) | A net15 | B net15 |
|---|---|---|---|---|
| all squares | −27.3 (−4.7, 555) | −27.0 (−4.3, 654) | −23.7 | −23.2 |
| squares, no 0.5% zone touch first | −24.1 (−4.1, 123) | −19.9 (−2.8, 174) | −33.5 | −28.8 |
| squares after a 0.5% zone touch | −28.2 (−4.1, 432) | −29.6 (−3.2, 480) | −20.8 | −21.2 |
| squares, no 1.0% touch | −24.3 (−4.2, 187) | −15.9 (−1.7, 269) | −33.2 | −22.5 |
| squares after a 1.0% touch | −28.8 (−3.8, 368) | −34.8 (−4.7, 385) | −18.8 | −23.7 |
| squares, no 1.5% touch | −24.4 (−4.7, 262) | −18.5 (−2.9, 361) | −31.2 | −21.3 |
| squares after a 1.5% touch | −29.8 (−4.2, 293) | −37.6 (−4.3, 293) | −16.9 | −25.5 |
| **zone alone (original rule), 0.5%** | −62.0 (−5.9, 208) | −26.5 (−3.0, 251) | **−113.5** | **−67.2** |
| zone alone, 1.0% | −52.6 (−4.9, 182) | −11.2 (−1.4, 208) | −103.6 | −59.7 |
| zone alone, 1.5% | −50.4 (−3.0, 145) | −4.5 (−1.1, 167) | −110.4 | −61.6 |

## Bottom line
- **Fails.** No zone + square cell beats random minutes; all are 20–38 bp below. Against random minutes, squares after a zone touch are
  *worse* than squares without one in both halves, and worse the deeper the zone.
- On raw net15 the zone squares are a few bp less negative in half A and mixed in half B. Every version still loses about 17–25 bp per
  trade after cost. Neither yardstick makes the combination a winner.
- **The original zone rule alone is the worst entry measured: −60 to −114 bp per trade.** Most names touch the zone (84% / 72% / 58%),
  and the touch is usually the start of the fade, so the limit buys the falling names. This matches round 4's buy-the-dip result.

## Corrected zone fill (2026-10-03, post-review, not pre-registered)

ZONE_FIX: fill at min(bar open, zone price) (a resting limit fills at the open when the bar gaps through), and skip
name-days already through the zone before 09:40 (a resting limit would have filled outside the window).

| arm | A lift bp (t, n) | B lift bp (t, n) | A net15 | B net15 | fills gapped through | skipped |
|---|---|---|---|---|---|---|
| ZONE_FIX 0.5% | −9.1 (−0.9, 141) | +7.1 (+1.3, 189) | −56.0 | −32.5 | 9% (mean 63 bp) | 132 |
| ZONE_FIX 1.0% | −0.7 (+0.2, 126) | +19.6 (+2.8, 156) | −49.2 | −28.6 | 7% (mean 79 bp) | 111 |
| ZONE_FIX 1.5% | +14.0 (+1.2, 100) | +30.5 (+2.0, 128) | −44.5 | −28.6 | 10% (mean 70 bp) | 87 |

- Against random minutes the zone rule is **no longer the worst entry**: lift is ~0 to +30 bp, positive in half B only
  (1.0% t 2.8, 1.5% t 2.0), not in half A. It would not pass the pre-registered bar (t ≥ 2 in both halves).
- **It still loses money**: net15 −29 to −56 bp per trade. The zone touch comes in hours when random minutes lose even more,
  so a positive lift is "less bad than buying then", not a profit.
- The lift now carries a passive-fill advantage the controls don't get (a limit fills below the bar open; controls enter
  at the open), so it flatters the zone rule. The −60..−114 bp in the table above was mostly the fill bug.
