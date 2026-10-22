# Book walls / room to run: stopped UNDERPOWERED at the count step (2026-10-22)

Prereg: [book_walls_prereg.json](book_walls_prereg.json) (amended after the result-skeptic's FIX FIRST, before any code).
Script: `tools/studies/book_walls.py` (78a1f4b7). Data: the Nasdaq mbp-10 books bought 10/4 (no new spend). **No wall
feature was ever compared with an outcome.** The count step reads outcomes only through within-name-day label
permutations, to project the noise floor.

**The idea (operator).** On Level 2, a big resting ask a few cents up is a ceiling that blocks a run, an empty ask side
is room to run, and stacked bids below are support. Tested here as:
- **H1:** OPEN vs WALLED. Is there an ask of at least 4x the typical level size and at least $25k within 15 bp above the
  best ask?
- **H2:** SUPPORTED vs UNSUPPORTED. The same test on the bid side.
- **Outcome:** the 5-minute mid return.
- **Comparison:** entries on the same stock on the same day.

**Why it stopped.**

| | H1 half A | H1 half B | H2 half A | H2 half B |
|---|---|---|---|---|
| Groups (favoured / other) | 491 open / 110 walled | 637 / 126 | 118 supported / 478 | 151 / 625 |
| Same-stock-same-day comparisons (floor 100) | 42 | 45 | 45 | 58 |
| Projected detectable effect (bar 5 bp) | 23 bp | 17 bp | 27 bp | 19 bp |

- **Population:** 2,056 of 7,261 entries are in it ($20+, spread ≤ 10 bp). 3,724 had wider spreads (these are gappers)
  and 1,481 were under $20.
- **Unclassifiable:** about a third of the population. These are $50+ names whose 10 visible levels don't span the 15 bp
  window.
- **Walls are uncommon:** about 1 in 5 classifiable entries has one in the window.
- **Too few comparisons:** with 3 random minutes per stock per day, only 42–58 stock-days per half have both kinds of
  entry.

**What it means.** This is no verdict on the idea. The data bought for the imbalance test cannot see it: too few
minutes per stock, mostly wide-spread gappers, and only 10 levels deep. The checks built in before any code mattered:
- **Spread:** with windows measured from the mid, a wide spread alone would have read as "open sky".
- **Odd lots:** without the $25k floor, a 40-share odd lot on a $400 stock would have counted as a "wall".

**What would test it.** Forward recording of the Nasdaq book (e.g. Webull OpenAPI depth=10, about $3/month) on the desk's
board names at **every** minute, not 3 a day. The noise floor shrinks with about 1/√n. Reaching 5 bp needs roughly 15–30x
more same-stock comparisons, which every-minute sampling on about 40 names gives in a few weeks. That is a spend
decision for the operator.
