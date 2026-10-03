# Skeptic review of recent studies (2026-10-02 evening)

Adversarial reviews by the `result-skeptic` agent (`.claude/agents/result-skeptic.md`, f1984ad), one agent per study.
The Mac mini was unreachable over ssh until ~19:58 ET, so the first seven reviews are **code-and-doc reviews**: data checks
are marked "not checked" in their reports, and any number below that a reviewer derived (rather than read from a doc) says so.
Month-end reran its numbers on Yahoo and, once the mini came back, on the Alpaca panel and SIP minute bars.

**Pending:** the overnight-edge settling rerun (pre-2016 + beta-adjusted alpha). This doc will be updated when they land.

## Summary

| Study | Decision in the doc | Verdict | Decision stands? | What the write-up overstated |
|---|---|---|---|---|
| Overnight edge (LONGER_HOLDS) | the one edge, ~16 bp/night | **UNPROVEN** | yes, keep running | alpha used the wrong benchmark; picked from 102 cells; no untouched period |
| Overnight variants | config already optimal | **PARTLY** | yes (no large gain exists) | "optimal" is a default: test could only see 4-17 bp gains; base chosen with OOS visible; -1% filter unproven |
| Overnight exit timing | keep the opening auction | **PARTLY SURVIVES** | yes | the t -3 came from the cost model; gross gaps are noise |
| Overnight book size | stay at 20 | **PARTLY** | yes, by default | no decision rule; 30/40 cost flips between halves; tail stats are single observations |
| Overnight account size | ~1,000 nights; $100 is a fill test | **PARTLY** (rerun done) | yes | no fees or auction cost; not the live sizer; nights for t=2 ~2x too high. Rerun: smallest sensible live size ≈ $1k |
| Month-end rebalancing (k=1) | build a score-only pilot | **UNPROVEN** | yes, score-only; no money | k=1 chosen after all four periods incl. pre-2016 were seen |
| Zone + square | fails; zone rule worst entry | **PARTLY** | squares: fail confirmed | zone fill model overpays; "worst entry" unsupported |
| Day desk (whole system) | — | **no positive expectancy** | n/a | ~−6 bp/trade since current gates; gap is mostly spread |
| Steady runway (60-day leans) | "up >2.35% + first 76 min" held both halves | **FAILS** | no — lean is dead | path odds came from overlapping moments; as money -2.1 bp gross per trade |
| Vol-pace runway (60-day) | runway findable; S7 gate dead | **SURVIVES / dead CONFIRMED** | yes | z +43 counted overlapping moments (clustered: day t 9.5, name-day z 7.7) |

Pattern: **no decision was overturned; nearly every write-up overstated certainty or omitted a cost.**

## 1. Overnight edge — UNPROVEN

- **Benchmark.** The +22.7%/yr alpha (t 1.9) was measured against SPY close→close. Against SPY's own close→open move
  (the right benchmark) the book's beta is **1.44**; 0.6 × (1/0.42) reconciles the two (SPY overnight has beta 0.42 to SPY
  close→close). 1.4 is the hedge ratio; 0.6 understated risk.
- **Alpha after beta and 4 bp cost** (reviewer-derived, approximate): **~+8 bp/night all years (t ~2.9), ~+8-9 OOS
  (t ~1.8-2.0), ~+3 IS (t ~0.9)**. Roughly half the 16 bp headline.
- **Multiple comparisons.** No prereg for LONGER_HOLDS; overnight was the winner of 102 cells, and the doc itself says t≈2
  is what chance produces at that count.
- **Survivorship.** Inactive names only from 2018; 2016-17 delistings missing; a name with no next open is dropped, not
  booked as a loss (lh_run.py `ok = np.isfinite(on)`).
- **Concentration.** OOS CAGR 30.2% → 17.6% without 2024 (SPY 9.2%); lost to SPY in 2019, 2021, 2023.
- **Look-ahead.** Ranking clean (`cf[t-21]/cf[t-252]`). The -1% filter backtest uses the full-day close
  (overnight_variants.py:67) — not known at the MOC cutoff. The 16 bp headline is unfiltered and clean.
- **Live.** The $100 book holds ~4.5 names, corr 0.76 with the backtest book; no real auction fill has been measured.
- **To settle:** a pre-2016 rerun (in progress) and logged real auction fills.

## 2. Overnight variants — PARTLY

- Holds: no variant shows a large gain; **3-1 and 6-1 are genuinely worse** (upper bounds -6.1 / -1.2 bp).
- **Power.** Pass bar +2 bp & t≥3 with paired SEs of 1-4.5 bp → minimum detectable gain ~4-17 bp/night (25-100% of the edge).
  Gains of 2-4 bp (F_OFF, N10, SPY_SKIP, ADV250M) cannot be ruled out.
- **Circular split.** Top-20/ADV $50M was picked in LONGER_HOLDS with the 2022-26 OOS printed; the -1% filter was picked on
  10/1 from OOS-only runs. In sample, 8 of 17 variants beat the base.
- **-1% filter ties no filter** (+0.9 bp, t 0.4). The 10/1 "+5 bp" likely came from eod_1555.py skipping nights with <5 names.
- Gaps: nights with <30 eligible names silently dropped; IS starts 2017-01-13 not the prereg's 2016-01-01; IS net not printed;
  prereg committed 57 s before results, without the script.
- **Doc fix:** "no variant shows a large gain; 3-1/6-1 worse; -1% filter ties no filter, unproven."

## 3. Overnight exit timing — PARTLY SURVIVES

- Holds: no later exit has a better gross point estimate, so even a zero-spread later exit wouldn't win.
- **The t -2.9..-3.7 is the cost model**: a constant ~14.7 bp (the 09:35 half-spread) charged at every later time
  (script line 101). Gross gaps are t -0.6..-1.4 — noise; "monotone giveback" is one cumulative path.
- Gross column not produced by the committed script; implies 3.2 bp cost vs 4 in code.
- "09:35" is ~09:36 (bar-start stamps); auction exit uses the bar open, not the official cross (~1 bp in its favour).
- Untested: limit/midpoint sells, hybrid "auction unless gapped up X%".
- **Doc fix:** "no later exit is better gross (t -0.6 to -1.4, not significant); market sells after the open add spread. Keep the auction."

## 4. Overnight book size — PARTLY

- Holds: **tail risk is market beta** (~1.4 at every size). The lever for the tail is a SPY overnight hedge (untested), not more names.
- **No decision rule** was fixed in d7f2584 (measures only). N30 -2.2 IS / -0.7 OOS; N40 -1.2 / -3.3 — neither loses both
  halves. Honest reading: no size is shown better; 20 kept by default.
- Tail metrics (worst night/month, maxDD) are single observations; CVaR gaps 0.2-0.6 pp reverse between halves.
- Confounded with the filter: N20 filter-off worst night (-11.4%) sits between N20 (-12.5%) and N30 (-10.5%).
- Cost charged only on non-empty nights (slightly favours N20). Doc's "Sharpe same or lower" is wrong for IS N40 (1.63 vs 1.53).
- Doesn't apply to the live $100 book (≤ ~4 names at ≤ $25).

## 5. Overnight account size — PARTLY

- Holds: no size proves the edge quickly; $100 is a fill test.
- **Fees not charged** (overnight_account_size.py:145 is gross). Assuming $0.01-0.02 per sell order (assumption, not
  in the repo): live $100 rule nets **~+3 to -1 bp/night**; break-even position ~$8-17; average live position ~$14. $1k ≈ +10 bp.
- **Not the live sizer.** Study `size_book` ≠ `size_paper` (overnight_book.py:377-413). Simulated at $1k: 9.0 names / 41% max
  weight (live) vs 11.2 / 33% (study). Max single-name weight 33-53% at ≤ $1k; worst-night risk unmeasured.
- **Nights for t=2:** used a 120-night sd of 253 bp. With the 10-year sd: ~460 nights gross, ~780 on OOS net.
- -1% filter not modelled at $100-$500; live prices on the 15:40 IEX trade, cash drifts with P&L.

### Settling rerun on the mini (live sizer + fees + filter, 10 years)

`/tmp/skeptic_size/size_live.py` on the mini (output `out2.txt`). 2,437 nights 2017-01-13..2026-09-24, 0 skipped, empty
nights = 0. Live `size_paper` / `size_live` imported from overnight_book.py; raw close for shares; adjusted close→next open.
Costs: 2 bp per auction leg + sell-order regulatory fee: **F1** SEC+TAF summed, rounded up, min $0.01; **F2** each rounded,
min $0.02 (rates from Alpaca's regulatory-fee page — an assumption). Filter "hybrid": 15:40 SIP price 2022+, full-day close
(look-ahead) 2017-21; swapping 15:40 in moved the full book only +18.3 → +18.5 bp.

| acct | names | dep | max wt | gross | net F1 | net F2 | sd | worst night | CVaR1% | corr | β SPY on | nights t=2 (F1/F2) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| $100 | 2.8 | 90% | 53% | +17.5 | +11.0 | +8.2 | 249 | -28.1% | -11.6% | 0.68 | 1.25 | 2038/3697 |
| $250 | 4.8 | 94% | 43% | +17.4 | +11.7 | +9.8 | 215 | -28.3% | -9.1% | 0.80 | 1.45 | 1348/1924 |
| $500 | 6.8 | 96% | 36% | +17.5 | +12.3 | +11.0 | 183 | -18.5% | -7.7% | 0.90 | 1.39 | 881/1112 |
| $1k | 9.4 | 96% | 27% | +19.3 | +14.5 | +13.6 | 181 | -14.3% | -7.7% | 0.93 | 1.47 | 623/712 |
| $2.5k | 11.8 | 97% | 19% | +19.0 | +14.7 | +14.2 | 174 | -13.9% | -7.1% | 0.97 | 1.45 | 565/602 |
| $10k | 12.6 | 97% | 12% | +18.4 | +14.2 | +14.1 | 168 | -14.2% | -6.9% | 1.00 | 1.40 | 562/570 |
| $25k | 12.6 | 97% | 11% | +18.2 | +14.0 | +14.0 | 167 | -14.2% | -6.9% | 1.00 | 1.40 | 564/567 |
| live rule | 2.7 | 41% | 17% | +12.2 | +8.0 | +5.3 | 125 | -10.6% | -5.0% | 0.66 | 0.52 | 981/2213 |

- **Smallest sensible live size ≈ $1,000** (9+ names, corr 0.93, 90-97% of the $25k net, worst night ~-14%). $500 is
  borderline (clears 75% of $25k net only under one filter version). ≤ $250 is a different 2-5 name book with 43-53% in one name
  and a worst night of -28%.
- **Fees matter only small:** F1 vs F2 is 2.8 bp at $100, 0.9 at $1k, 0 at $25k; the 4 bp auction cost is larger at every size.
  Fee = whole net edge at ~$7 (F1) / ~$14 (F2) per order. Average order at $1k is $135.
- **Nights for t=2 on net:** ~565 at $2.5k+, 623-712 at $1k, 2,000+ at $100 (corrects "~1,000 at any size").
- **Names held ≈ 12.6, not 20, even at $25k** — the -1% filter drops ~37% of picks.
- **The live rule** sits out 300 of 2,437 nights, deploys 41%, nets ~+5 bp (~$0.05/night): a fill test only.
- These nets are **not beta-adjusted** (β ~1.4); see section 1 for alpha.
- Not checked: panel survivorship; auction fill rate / partials. The 15:40 price is SIP 5-min (live uses IEX last trade).

## 6. Month-end rebalancing — UNPROVEN

Reran on Yahoo 2002-08..2026-09: k=1 +21.4 bp, t 3.82, n 290 (doc +20.6, t 3.69); and on the mini's Alpaca panel
(true month-ends 2016-02..2026-08, n 127): +20.6 bp, t 2.53.
- **Signal look-ahead: PASS.** S to T-2 flips 17/290 signs, +18.9 bp t 3.35 (Alpaca 10/127, +18.7, t 2.29); a 15:30 S flips 0/35.
- **Multiplicity: PASS.** 15 cells per period; Bonferroni ×15 on t 3.69 still p~0.003. Positive at every k (k1-k5 t 2.6-3.8);
  not a knife edge.
- **Independence: FAIL.** Alpaca and Yahoo results came from one run (94e06c9, 2 min after the prereg), so k=1 was chosen
  after seeing pre-2016. No untouched data remains. Harvey et al. headline uses N=5, not k=1.
- **Bug:** the series end (2026-09-25) is counted as a month end (month_end_rebal.py:43) → a fake -56 bp trade. Without it:
  +21.7 bp, t 3.86.
- **Concentration: PASS** (drop top 5 months +15.5, t 3.09; ex-2008/2020 +19.0). Weak spot: 2010s +9.9 bp, t 1.24.
  Short side +20.4 (t 3.41), long +22.9 (t 2.14).
- **Live path: not what the backtest scores.** Market mode sends DAY market orders at close-2m for entry and exit
  (month_end_book.py:261-263, 289-291); the score uses official crosses minus 2.26 bp (:197-200). Priced on SIP minute bars
  over the last 36 events: 15:58→15:58 made +32.0 bp gross vs +37.0 at the 15:59 close, **~-5 bp (sd 14.6) plus spread** —
  small. Live signal/dates/sign replayed on 25 months match the study. No short borrow charged (likely <1 bp).
- **To settle:** ~24+ forward months scored on the plan, preferably with the pilot in auction mode.

## 7. Zone + square — PARTLY

- **Squares fail: confirmed** on code (prereg adherence, next-bar-open entry, windows, cost, day-clustered t all pass).
- **Zone-only numbers unreliable.** Fill always booked at the zone price (line 97) even when the bar opened below it; touches
  before 09:40 count as "first touch" but fill only in-window, so many fills are at a price already left behind. Likely
  inflates the -60..-114 bp. "Worst entry measured" unsupported; buying a falling name is still plausible.
- **To settle:** fill = `min(o[k], lvl)`, require price above the zone at 09:40, report the share of gap-through fills.

## 8. Vol-pace runway (60-day) — findable SURVIVES; S7 dead CONFIRMED

Reran on the mini (`ai_reports/vol_pace_60d_rows.pkl`, `combined_spreads.json`).
- **Clustering.** Moments are 15 min apart with a 60-min label (S7 averages 13.1 per name-day). The z +43 treated them as
  independent. Clustered: high vs low pace tercile run2 +4.1 pts, **day t 9.5 (58/60 days), name-day z 7.7**. Still real.
- **It is volatility, not direction.** fwd60 high vs low pace -1.0 bp (t -0.5); vol-excess -0.05 bp (t 0.0). Inside the calm
  volatility tercile pace adds nothing (run2 t 1.1); in the jumpy tercile runk and symk rise together (t 5.6 / 6.0).
- **Spread is SIP, not IEX** (5 s median before the moment): the IEX overstatement doesn't apply. 16.6 bp is the mean;
  median 8.6, p25 4.0. Break-even spread = S7 gross, +0.8..+3.9 bp — below even the p25. S7 moments with spread ≤5 bp
  still net -2.1 (fwd60 gross -0.7): the gross lift lives in the wide names.
- **Feed.** Both sides SIP and causal, but the share-of-day curve includes 5% pre-market while the numerator excludes it
  (median pace 0.56). **Live rvol_pair includes pre-market, so 1.64 is not on the live scale.**
- **Universe.** 60-day universe = names the book traded in September (survivorship + hindsight, different population from
  the original). Affects all terciles alike, so it can't create the lift.
- n: 60 days, 268,461 moments, 250-278 names/day, no missing pace. fwd60 is close-to-close, so neither exits nor spreads
  can hide a directional edge.

## 9. Steady runway (60-day leans) — FAILS

Reran on the mini (`/tmp/sk4.py`, `/tmp/sk5.py`, SIP 1m, 0 fetch errors).
- **Cuts:** 2.35% and the early-minutes cut came from 8-day terciles (`steady_runway.out:28,36`), so fair out of period;
  the combination was committed before the 60-day run (bdcb91c 18:28, rows 18:55). But 13 leans were tested on the 60 days
  with no prereg, and "<$10" is in-sample (the 8-day lean pointed the other way). Days binomial: 34/60 p 0.18, 37/60 p 0.046,
  43/60 p 0.0005 — with 13 tests, (a) is noise by its day count.
- **Look-ahead: PASS** (`day_chg = c[i]/prev_close` at the minute; path scan from bar i+1; prior close from earlier daily bars).
- **Overlap: FAIL for (a).** Per name-day, (a) climbs 15.15% vs falls 15.66% (edge -0.52%); first moment only 4.78 vs 4.27;
  clustered by day t 1.39, top-3 days drive most of it. (b) "first 76 min" t 0.76 → fails. (c) "<$10" t 3.62, survives as path odds.
- **Universe: FAIL.** The 60-day universe is the 9/16-9/25 recorded cache projected backward (survivorship + hindsight);
  SIP while live is IEX.
- **Money: FAIL.** First (a) moment per name-day, next-bar-open entry, exits +5% / 1% stop / 2% give / 15:55, adverse-first:
  n 10,617, **gross -2.1 bp**, win 32%, day t -1.59, halves -3.4 / -0.7; net -12 bp at 10 bp spread. ≥$10 -3.6 gross; <$10
  +2.7 gross but -4.8 in half 1 and negative after its wider spread. Versus a random other early name at the same minute (-18.3)
  (a) is +15 bp/day (t 3.79): the screen avoids faders, it doesn't make money. **The planned 2%-give ratchet test is answered: negative.**
- Not checked: SPY beta; one-notch sensitivity of the cuts.

## 10. Day desk as a whole (2026-10-03)

Read-only on the mini (`ai_reports/outcomes.jsonl`, entry/exit prices, day-clustered t).
- **Verdict: no demonstrated positive expectancy after costs.** All 1,001 closed day trades 8/1-10/2: mean −27.2 bp,
  median −12.2, win 27%, day t −4.19 (−$1,011 paper). Since the current gates (9/29-10/2, ≥ $10): n 199, **−6.0 bp**,
  median −0.5, win 39%, day t −2.34, all four days negative. With a ~10-11 bp round trip, recent gross is ≈ +4 bp
  (inferred, not measured): **the remaining gap is mostly the spread**; earlier it was half spread, half selection.
- **By price (monotone):** <$5 −157 bp (33), $5-10 −52 (190), $10-20 −26 (298), $20+ −9.5 (480).
- **What works (loss reducers; none make it positive):** $10 floor (live; beats all fills in every period); SIP spread
  gate 0.2% (live, mechanism strongly supported) **but `ai_watch_momentum_spread_exempt` is still True**, so momentum
  names skip it (ai_entry_watch.py:8019-8021), against the 10/1 decision; gap-down block (live; 8 days, overlap not
  checked); range-position cap 90 (live; one 10-day sample); tight leash (−13 vs −29.5 bp with a 600 s minimum hold).
- **"Exits cannot move the mean": reliable, wording wrong** — exits move a negative mean toward zero; they cannot create a
  positive one. **"Entry setups are noise-equivalent": reliable at ~±3 bp on liquid names** (later pre-registered SIP work).
- **Unreliable, need a rerun:** "desk entries 12.7 bp worse than random" (old config/universe); "universe fades at every
  horizon" (old microcap list); momentum −0.066R (label contaminated); spread-exempt +4.4 bp (one day); any lifetime split
  (39+ config commits).
- **Ranked next steps (free data):** (1) hold config ~10 sessions and score the live three-arm entry test (ask/mid_down/
  bid), graded against the SIP mid — ceiling ~+5 bp; (2) replay the last 5 days with the momentum exemption off and the
  SIP cap at 0.05-0.10% (expect +3-4 bp); (3) rerun Round-1A (desk vs same-name random) on 9/29+ fills — is selection still
  a penalty or is it cost alone?; (4) grade the gap-down and range-position gates on logged refusals, clustered by
  name-day; (5) intraday (open→close) leg of the overnight top-20 names over 10 years — names to avoid by day?
- **Realistic ceiling if (1)-(2) land: about −2 bp/trade — break-even territory, not profit.**
- **Pillars:** opportunities ~1.0-2.3 fills/10 min (clears ≥1, short of 5); entry quality fails (no setup beats a
  random minute); runway findable as volatility, not direction; capture at its ceiling (median hold 109 s, leash ~optimal).

## Fixes made 2026-10-03

1. `month_end_rebal.py`: the last data bar no longer closes a month (fake −56 bp 2026-09 trade removed); the Yahoo
   slice keeps one 2016 bar so 2015-12 still closes. Rerun on the mini: k=1 holdout A +36.3 (t 2.11, n 25); pre-2016
   unchanged (161 months).
2. `zone_square_study.py`: new ZONE_FIX arm (fill at min(open, zone), skip names through the zone before 09:40). Lift
   ~0 to +30 bp (half B only), net15 still −29 to −56 bp: the "worst entry" claim was the fill bug; the rule still loses.
3. Correction notes at the top of OVERNIGHT_VARIANTS, OVERNIGHT_EXIT_TIMING, OVERNIGHT_BOOK_SIZE,
   OVERNIGHT_ACCOUNT_SIZE, ZONE_SQUARE and MONTH_END_REBAL.
4. Month-end auction mode: **no change** — market mode on paper is deliberate (Alpaca paper runs no auction); the score
   already uses official crosses; auction mode exists for a real account.

## Decisions (user, 2026-10-03)

- **The −1% overnight filter is OFF** (`OVERNIGHT_INTRADAY_MIN` default "off"); the would-be drops at −1% are logged
  nightly as `filter_shadow` ledger rows.
- **Live overnight account stays at $100** (fill test); not ready for a ~$1k account.

## Doc and memory corrections queued

- OVERNIGHT_VARIANTS, OVERNIGHT_EXIT_TIMING, OVERNIGHT_BOOK_SIZE, OVERNIGHT_ACCOUNT_SIZE, ZONE_SQUARE: reword as above.
- MONTH_END_REBAL: fix the series-end bug; note k=1 saw pre-2016.
- runway-is-findable memory: replace "z +43" with clustered day t 9.5 / name-day z 7.7; spread is SIP mean 16.6 / median 8.6;
  S7 threshold not on the live pre-market-inclusive scale.
- steady-runway-definition memory: lean (a) FAILS per name-day and as money (-2.1 bp gross); (b) fails; (c) <$10 is in-sample
  path odds only; 2%-give ratchet test answered (negative). Keep the user's runway definition itself.
- Memory: overnight edge ≈ 8-9 bp/night net of beta (1.44) and cost, unproven out of period; -1% filter unproven;
  month-end pilot is score-only and its market mode doesn't match the scored path.
