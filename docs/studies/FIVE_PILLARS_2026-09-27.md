# Five Pillars name-criteria study (2026-09-27)

**Question.** Jonathan's new prime directive is to serve the best names to the book, early. Entries and exits stay unchanged. Can Ross Cameron's "five pillars" name criteria be tuned so the names they pick have capturable runway after the spread from 09:30 to 11:00? The pillars are:
- price $2–20;
- up ≥10% from the prior close;
- relative volume ≥5×;
- a news catalyst;
- float under 10M.

**Answer: no.** None of the 540 threshold cells has positive net in both halves under any of the three entry types.
- Gross 30-minute drift after qualifying is about zero (roughly −40 to +30 bp for most cells).
- Net therefore lands at about minus the quoted spread: −70 to −250 bp.
- The strict five-pillar cell yields about 6.9 names a day, most of them already identifiable premarket, and they lose about 150–240 bp net.
- Tightening the criteria mostly selects names with *wider* spreads, not more drift.

Script: `tools/studies/five_pillars_study.py`, with phases daily / minute / sn / float / analyze / report. Raw outputs are in `/tmp/fp` on the mini.

## Data and method
- **Universe.** 11,193 currently active, tradable common stocks on NYSE/NASDAQ/ARCA/AMEX/BATS.
  - Filtered with `ticker_filters.is_common` and `is_levered_etp`; warrants, units, rights and OTC are excluded.
  - Daily bars: Alpaca SIP, split-adjusted, 2025-09-01 to 2026-09-26.
  - Candidate name-days: prior close $1–25 and daily high ≥ +10%. That gives 46,006 name-days over 248 sessions (2025-10-01 to 2026-09-25).
- **Minute bars.** 1-minute SIP bars from 04:00 to 11:31 for every candidate.
  - 23,834 name-days reached ≥ +10% by 11:00 at $1–25.
  - 13,497 were "loose": at some real minute they were $2–20, ≥ +10% and RVOL ≥ 2. That is a superset of every grid cell.
  - 11,592 of those had an SIP quote sample and were analysed. The other 1,905 (14%) had no quote in the sample windows and were dropped.
- **Point-in-time features at each minute's close** (no lookahead):
  - price;
  - % change versus the prior close;
  - RVOL = cumulative volume since 04:00 ÷ (ADV20 × f(t)).
    - f(t) is a typical cumulative intraday volume curve: median cumulative volume ÷ daily-bar volume over 1,327 non-mover name-days (|chg| < 3%) from the earlier momentum study sample.
    - f(09:29) = 0.011, f(09:59) = 0.125, f(10:59) = 0.266. The curve is floored at 0.001.
    - Early premarket RVOL is therefore noisy and generous (see caveats).
- **Float.** Current float only; there is no float history. See caveats.
  - Source: the desk `float_cache`, read-only, where present (788 symbols); otherwise `float_feed` via Finnhub (1,667 symbols).
  - Known for 11,169 of 11,592 name-days (96%). Name-days with unknown float are excluded from float-filtered cells.
- **News.** Alpaca historical news, all of it from benzinga.
  - Window: from 16:00 on the prior session to the decision minute.
  - 5,484 of 11,592 name-days (47%) had at least one item by 11:00.
- **Qualify minute.** The first real minute (premarket or RTH) at which all of the cell's thresholds hold.
- **Entries.** Decisions from 09:29 to 10:59; entry at the next bar's open plus half the spread. A cell's names are entered only after their qualify minute; premarket qualifiers start at 09:30. Three entry types:
  - (a) *cross*: the EXH fast Williams %R crossing up through −50 (`momentum_runway_study.crosses_on`, the desk-arm proxy used in earlier studies);
  - (b) *rand*: every minute after qualifying (equivalent to random minutes);
  - (c) *qbuy*: one buy at the qualify minute, or at 09:30 for premarket qualifiers.
- **Cost.** Real SIP quoted spread for each name-day, sampled at 09:31, 09:45 and 10:15 (median over 15 s windows); the nearest sample is used.
  - Net = gross − half-spread at entry − half-spread at exit, so a full round trip.
- **Metrics.**
  - gross30: 30-minute close-to-close from the entry bar's open, before costs.
  - end30: net after 30 minutes.
  - hold11: net held to the 11:00 close.
  - MFE30.
  - +20 / +50 bp before −35 bp hit rates, with the reverse check (−50 before +35).
  - Most names are under $5 or have spreads around 100 bp, so the bp hit rates are nearly meaningless: the entry half-spread alone often trips −35. **Read net and gross.**
- **Grid.**
  - price {$2–5, $3–8, $5–10, $10–20, $2–20}
  - × gain {10, 20, 30%}
  - × RVOL {2, 5, 10}
  - × float {<10M, <20M, <50M, any}
  - × catalyst {yes, no, any}
  - = 540 cells × 3 entry types = 1,620 tests.
  - Train half A = alternate sessions (124 days); test half B = the other 124.
  - t-stats are computed over per-day means, equal-weighting days.
  - With 1,620 correlated tests, pure luck would produce a best |t| of about 3.5. Bonferroni at 5% needs |t| ≳ 3.9.
  - **No cell has a positive test t at all**, so the correction is moot.

## Compact table: test half (B), net bp after the full quoted spread
| Cell | names/day | premarket-qualified | qbuy end30 / hold11 (gross30) | %R cross end30 / hold11 (gross30) | random end30 / hold11 (gross30) |
|---|---|---|---|---|---|
| Loosest: $2–20, ≥10%, ≥2×, any float, any news | 46.7 | 49% | −175 / −169 (+14) | −80 / −83 (−3) | −116 / −118 (+4) |
| $2–20, ≥10%, ≥5× | 31.0 | 67% | −178 / −170 (+12) | −88 / −94 (−5) | −116 / −118 (+5) |
| $2–20, ≥10%, ≥5×, float <10M | 13.5 | 73% | −243 / −254 (+23) | −143 / −155 (−26) | −178 / −192 (−4) |
| **Strict five pillars**: $2–20, ≥10%, ≥5×, <10M, news | **6.9** | **82%** | **−197 (t −3.4) / −241 (t −3.7) (−7)** | **−147 / −162 (−42)** | **−147 / −171 (−11)** |
| Best cell by train (qbuy): $2–5, ≥30%, ≥10×, <10M, no news | 1.65 (small n: 201 events per half) | 81% | train +94 / +22 → test −267 / +6 (−107) | test −37 / +6 | test −64 / +2 |

Strict cell on the train half (A): qbuy −166 / −136, cross −136 / −73, random −137 / −129. The result is the same in both halves.

**Search summary.** Across the 540 cells with n ≥ 30 in both halves:
- end30 > 0 in both halves: 0 cells for each entry type.
- hold11 > 0 in both halves: 1 qbuy cell, 0 cross, 0 random.
- Best test-half t for hold11, per entry type: −0.35 (qbuy), −0.22 (cross), −0.35 (random).
- The cells that looked best in training ($2–5 names up ≥30% with no news) flipped sign in the test half.

## Which thresholds matter
One threshold is varied at a time, with the others at their loosest. Figures are the random-minute net and gross, pooled over both halves:

| Pillar | Levels: end30 net / gross30 (bp) | What happens |
|---|---|---|
| Price | $2–5: −117 / +15<br>$3–8: −116 / +10<br>$5–10: −114 / +3<br>$10–20: −112 / −7 | Cheaper names drift slightly more gross, but their wider spreads eat all of it. |
| Gain | ≥10%: −114 / +7<br>≥20%: −122 / −3<br>≥30%: −115 / −13 | Bigger gappers have *less* drift left. The qbuy gross falls from +32 to −88. |
| RVOL | ≥2×: −114 / +7<br>≥5×: −113 / +8<br>≥10×: −110 / +10 | Hardly matters. It mainly makes names qualify earlier: 49% → 82% premarket. |
| Float | <10M: −183 / +6<br><20M: −176<br><50M: −157<br>any: −114 / +7 | Low float adds **no** drift and **doubles** the spread. Median spread is 158 bp for <10M names versus about 90 bp for all names. This is the most damaging pillar for net. |
| News | yes: −93 / +2<br>no: −126 / +7 | News names are less bad, because their spreads are tighter, not because they drift more (qbuy gross +27 with news vs +33 without). |

The %R cross looks less bad than random minutes: −74 vs −114 net in the loosest cell. Its gross is the same, about 0 (+4 vs +7). The gap comes from crosses concentrating in tighter-spread, more liquid names, not from any edge.

## Names per day and how early
Strict five-pillar cell: 1,703 name-days over 248 sessions, mean 6.9 per day (median 6); every session had at least one. Cumulative names qualified, per day on average:

| By | 08:00 | 09:29 | 10:00 | 10:30 | 11:00 |
|---|---|---|---|---|---|
| Names qualified | 3.6 | 5.7 | 6.4 | 6.7 | 6.9 |

- 82% qualify premarket. Qualify-time quartiles: 04:48 / 07:41 / 08:57.
- At least 3 names qualified by 09:30 on 223 of 248 days; at least 4 on 194 of 248 days.
- So the strict filter *can* fill a book of 3–4 concurrent opens from the open on most days. The names just have no net runway.
- Median quoted spread for strict names: 97 bp (p75 218 bp). Median price at qualify: $5.60.
- Looser cells yield 13–47 names a day.

## Caveats and data gaps
- **Float is current, not historical.** Floats change through offerings, reverse splits and so on, so the float filter carries lookahead and misclassification. 4% of name-days have no float.
- **Survivorship.** The universe is only currently active symbols; names delisted during the year are missing. That probably flatters results, and the results are negative anyway.
- **Candidate stage uses daily bars.** If Alpaca daily bars reflect the RTH session, names that were up ≥10% only premarket and never in RTH could be missed at the candidate stage. Premarket minute data itself comes from SIP extended hours and was present.
- **Premarket RVOL is noisy.** With the curve floored at 0.001, a small early print can pass RVOL ≥ 5×. Premarket qualify times are therefore early and generous. That only affects *when* a name qualifies; outcomes start at 09:30.
- **Spreads are three 15-second samples per name-day.** 14% of loose name-days had no quote in those windows and were dropped, which probably leaves out the thinnest names.
- **News.** Alpaca / benzinga only. Items per request were capped at 50 per 40-symbol batch; spot checks came back under the cap, but a few busy batches might have been truncated. The first session (2025-10-01) was re-fetched with the correct prior-day window.
- **Hit rates** are conservative: when the target and the stop fall in the same minute, it counts as a miss. Trust net and gross.
- **t-stats** are per-day and equal-weighted, while the reported means are event-weighted, so their signs can differ for tiny cells.
