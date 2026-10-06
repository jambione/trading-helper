# Counterfactual: sell halfway to resistance, or at 3:55 (2026-10-06)

Pre-registered and pushed before the replay: [`sr_half_gap_counterfactual_prereg.json`](sr_half_gap_counterfactual_prereg.json)
at `11d9ad4`. Script `tools/studies/sr_half_gap_counterfactual.py`, run on the mini the same day.
**What-if on the already-mined tape. Not a pass. No config change.** Queue #5 is a different test and was not touched.

Opens are the rising-%R squares (E rows) from 2026-09-04 through 2026-10-02. A wide gap is room of at least 1% to the nearest charted resistance, the size of the ASTS and RKLB gaps on the 10/06 charts. A modest gap is 0.30% up to 1%. The wall under 0.30% is left out. The exit sells at halfway to that resistance, or at the 3:55 close if price never trades through halfway. Cost is a flat 0.20% round trip. The random minute is the same name and the same hour, with the same kind of exit off its own resistance, and it also has to have at least 0.30% of room.

433 squares had a gap of at least 0.30%. 1,104 had no resistance above the entry. 272 were on the wall and were left out. 189 were wide, 244 were modest.

| | Half A (09-04..09-18) | Half B (09-21..10-02) |
|---|---|---|
| Wide, n (days) | 37 (5) | 152 (10) |
| Wide gross / net | −33.5 / −53.5 bp | −2.3 / −22.3 bp |
| Hit halfway | 43% | 58% |
| Median room | 2.32% | 1.55% |
| Wide minus random | −74.7 bp (t −1.57) | −26.4 bp (t −2.19) |
| Random gross | +41.2 bp | +24.0 bp |
| Modest, n | 36 | 208 |
| Modest gross / net | +15.5 / −4.5 bp | −5.0 / −25.0 bp |
| Modest hit halfway | 83% | 79% |
| Modest minus random | −10.4 bp (t −0.89) | −21.1 bp (t −2.36) |
| Wide minus modest | −49.1 bp | +2.6 bp |

Half A has five days, so that column is thin. Half B is the one with a real count. There, a wide gap and a halfway sale finished 26 bp behind a random minute, and the net was −22 bp. The modest gaps tagged halfway about 80% of the time and still finished behind the random minute, −21 bp in half B. Covering halfway was common. It did not pay for the spread, because the trades that never got there were held to 3:55 and those losses were larger than the tags.

Selling at the full gap instead of halfway, on the wide names only, was about the same in half A (−0.2 bp) and 16 bp better in half B (t +1.34). That cell is information. The full-gap exit on all squares already failed its own test.

SIP bars, flat 20 bp, and a sample the queue has already used. A better-looking cut of these same days would still not be a reason to arm it.
