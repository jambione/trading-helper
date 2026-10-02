# Zone + square: squares after a dip into a zone below the book-entry price (2026-10-02)

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
