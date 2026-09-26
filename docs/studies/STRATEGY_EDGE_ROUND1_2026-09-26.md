# Strategy edge study, round 1 (2026-09-26)

Read-only study. Nothing live was changed: no config edits, no restart, no commit, no push. Scripts are in `tools/studies/edge_*.py` (uncommitted). The raw console output is in `docs/studies/STRATEGY_EDGE_ROUND1_2026-09-26_raw.txt`.

## Bottom line

- **No tested idea is net-positive out of sample.**
  - C: 0 of 120 holding-period cells had net > 0 in both IS and OOS. The best cells are about 0 bp: liquid names 10:30–12:00, 30–60 min holds, OOS −0.2 to +0.4 bp against IS −1.5 to −0.3 bp.
  - D: none of the published intraday effects we tested passes on this sample.
- **Gross edge is about 0 everywhere.** On the current $20–100 universe, random entries at every hold from 5 min to 15:50 gross between −8 and 0 bp. The %R −50 cross grosses the same.
  - Net is roughly minus the spread: −15 to −27 bp per trade at the mean spread of 15–19 bp. The median spread is 8.4 bp; the tail is fat.
- **Cost is the lever you control, but it only takes the loss down to about zero.**
  - Liquid large caps and ETFs with a prior-day spread ≤ 3 bp: net −2.2 to −2.4 bp per trade (5–60 min holds, IS and OOS). The current universe loses −15 to −21 bp at the same holds, so this is about 8x less loss. It is still not positive.
  - Entering passively recovers only about 6 of the 15–19 bp: fills are adversely selected (see B2).
- **Real desk fills, 9/16–9/25 (A): about half of the loss is cost and half is adverse price movement.**
  - The desk's entries lost 12.7 bp mid-to-mid over a 7-minute average hold, where random entries lose about 0. Most of that comes from exits within 5 minutes and from names under $20.

## Data and method

- **Window:** Claude's Stage-B 60 trading days, 2026-06-22..2026-09-15. The split was fixed before looking: **IS = first 40 days (06-22..08-17), OOS = last 20 (08-18..09-15)**. Nothing is fitted, so IS/OOS is a stability check.
- **Bars:** Alpaca **IEX** 1m bars (free feed), 04:00–16:05. Fetched with `edge_fetch.py` using the `tools/bars.py` client, at ≤ 1 multi-symbol request/s, and cached in `ai_reports/edge_iex_bars/DAY.pkl` on the mini.
  - Prices are the last IEX print at or before each minute, forward-filled. An entry needs a print ≤ 5 min old.
  - **Validation:** on 38,635 of Claude's Stage-B moments, the IEX forward 30m return averaged +1.3 bp, the same as Claude's SIP fwd30 (+1.3 bp). Correlation 0.958, median absolute difference 4.3 bp.
- **Spread model (same as Claude's `combined_score_study.py`):** SIP quoted spread (ask−bid)/mid, median over the few seconds before the moment. The **full spread is charged once per round trip at entry** (half in, half out).
  - Sources: Claude's cached 44.9k samples (`combined_spreads.json`, 09:45–15:15), plus 59.8k new samples in `ai_reports/edge_spreads.json`:
    - the liquid list at 8 times a day;
    - the current universe at 09:36 and 15:45, to cover the open and the close.
  - The spread for any (name, day, time) = that name-day's median sample × a time-of-day profile: 09:30–10:00 ×2.21, 10:00–10:30 ×1.44, then ×1.00, and 15:30+ ×0.76.
  - These are historical SIP *quotes*. Alpaca's free plan serves them once they are 15 minutes old, and it is the same lookup the nightly `exec_report` runs. No data was paid for and nothing live touches SIP.
- **Universes:**
  - **CUR** = Claude's Stage-B universe: each day, the most-traded $20–100 names by the prior day's dollar volume (~259 a day, drawn from ~759 names the desk nominated in September).
  - **LIQ** = a fixed list of 100 large caps and ETFs written down before any results; **LIQ≤3** = those whose *prior-day* median SIP spread was ≤ 3 bp (~40–45 a day; median spread 1.9 bp).
- **Entries:**
  - **random** = every 5 minutes from 09:35 to 15:30 in every name: the random-entry baseline, in the same names and time windows.
  - **wr** = the live trigger on IEX 1m bars (prior session prepended for warm-up): fast %R(21, EWM 7) crosses up through −50 while slow %R(112, EWM 3) is above its value 2 bars earlier; at most one per name per 15 min.
- **Statistics:** mean gross, mean net, median, win rate. The t-stat is **clustered by day** (mean of daily means over their standard error). The 95% CI is a bootstrap that resamples days.

## A. Where the real desk loss goes (fills 9/16–9/25, 314 round trips)

Source: `edge_a_cost_decomp.py`. Each trade is split into the mid-to-mid **move** (SIP mid at the sell submit ÷ SIP mid at the buy submit) and the **cost** paid against those mids (spread plus excess slippage).

| | n | gross bp | move bp (95% CI) | cost bp | of which spread | excess | P&L | move $ | cost $ |
|---|---|---|---|---|---|---|---|---|---|
| ALL | 314 | −26.7 | −12.7 (±12.4) | +14.1 | 21.1 | −7.0 | **−$245.94** | −$121.87 | **$123.82 (50%)** |
| price <$20 | 152 | −42.2 | −23.0 | +19.3 | 33.6 | −14.3 | −$172.71 | −$98.78 | $73.60 |
| $20–30 | 42 | −9.2 | −0.2 | +9.0 | 8.2 | +0.8 | −$12.56 | +$0.45 | $13.03 |
| $30–50 | 56 | −18.4 | −7.9 | +10.5 | 11.0 | −0.4 | −$34.33 | −$12.98 | $21.40 |
| $50–100 | 38 | −13.4 | −4.0 | +9.5 | 10.6 | −1.1 | −$20.05 | −$8.53 | $11.54 |
| $100+ | 26 | −2.2 | +4.4 | +6.6 | 6.6 | 0.0 | −$6.29 | −$2.04 | $4.26 |
| held 0–5 min | 206 | −38.3 | −22.5 | +15.9 | 27.3 | −11.3 | −$229.35 | −$147.48 | $82.75 |
| held 5–15 min | 63 | −3.0 | +10.0 | +12.6 | 10.2 | +2.3 | −$0.47 | +$29.59 | $28.93 |
| held 15–30 min | 28 | +1.2 | +9.1 | +7.9 | 8.6 | −0.7 | −$5.09 | +$2.13 | $7.21 |
| held 30+ min | 17 | −21.1 | −13.3 | +7.8 | 7.7 | +0.1 | −$11.03 | −$6.11 | $4.93 |

How to read it:
- **Cost is 50% of the loss overall.**
- **On $20+ trades (162) cost is 68% of the loss:** move −$23, cost $50, P&L −$73. Per trade that is about −3 bp of move against about 9.3 bp of cost.
- On 9/24–25, under the current $20 floor, cost was about 60% of the loss (8–9 bp cost per trade against −5 to −6 bp move).
- The average loss was −$30.7/day, of which cost was about $15.5/day.
- The 8-day total of −$245.94 roughly matches the scorecard (−$271.58); the gap is 8 unmatched sells and orders without a quote.
- **Spread paid (round trip, bp) by price:** <$20 33.6 · $20–30 8.2 · $30–50 11.0 · $50–100 10.6 · $100+ 6.6.
  - "Excess" is negative, meaning fills were better than the quoted mid ± half. So the cost is the spread, not slippage beyond it.
- **The move is the other half:**
  - The desk's entries moved −12.7 bp mid-to-mid; random entries at 5–15 min gross about 0. That is about −2σ.
  - It is concentrated in exits within 5 minutes (206 trades, −22.5 bp) and in sub-$20 names.

## C. Holding-period grid: net bp per trade, IS | OOS

The full 120-cell table (with day-clustered t) is in the raw file. The table below shows the all-bucket rows plus a summary of the best buckets. "wr − matched" is the %R trigger's net minus the mean random net in the **same name-day and entry bucket**.

| Universe | Entry | 5m | 15m | 30m | 60m | 120m | to 15:50 | trades/day (OOS) |
|---|---|---|---|---|---|---|---|---|
| CUR | random | −19.1 \| −15.4 | −19.4 \| −15.8 | −20.1 \| −16.4 | −21.3 \| −17.0 | −21.2 \| −19.0 | −27.0 \| −21.7 | (grid) |
| CUR | wr | −14.4 \| −11.7 | −13.7 \| −12.0 | −13.8 \| −12.5 | −16.1 \| −14.2 | −14.7 \| −15.8 | −22.3 \| −18.6 | ~1,050 signals |
| CUR | wr − matched | −0.8 \| −0.9 | −1.5 \| −2.1 | −3.4 \| −4.2 | −8.0 \| −8.1 | −8.4 \| −9.0 | −8.1 \| −7.8 | |
| LIQ≤3 | random | −2.2 \| −2.2 | −2.2 \| −2.2 | −2.3 \| −2.3 | −2.3 \| −2.4 | −2.3 \| −2.9 | −5.1 \| −6.6 | (grid) |
| LIQ≤3 | wr | −2.5 \| −2.6 | −2.0 \| −2.5 | −1.8 \| −2.5 | −2.0 \| −2.5 | −2.6 \| −4.3 | −5.5 \| −8.2 | ~250 signals |
| LIQ≤3 | wr − matched | −0.5 \| −0.6 | −0.4 \| −0.7 | −0.7 \| −1.0 | −2.1 \| −1.7 | −2.9 \| −2.8 | −2.5 \| −2.1 | |

- **Gross:** CUR random is −0.1 to −3 bp at 5–120 min and −8/−6.5 bp to 15:50; %R is the same. LIQ≤3 is about 0 to −0.7 bp at 5–120 min.
- **Entry-time buckets:** 09:35–10:30 is the worst everywhere (CUR random −30 to −43 bp net, because the open spread is about 2x). The best CUR bucket is 10:30–12:00 (−12 to −17). The best LIQ≤3 cells are 10:30–12:00 at 30–60 min: random −1.5\|−0.2 and −1.2\|+0.2; wr −0.3\|+0.4 and −0.6\|+0.4.
- **Cells with net > 0 in both IS and OOS: 0 of 120.**
- **The %R trigger "beats random" only by composition.**
  - Raw %R net is 4–6 bp better than the random grid, but only because it fires more often in names with more IEX prints, which have tighter spreads.
  - Against random entries in the same name-day and bucket, it is **worse** by 1–9 bp, most of all at 60 min to 15:50 (t −10 to −17, both halves).
  - Buying a %R up-cross buys a short-term bounce that then reverts. This agrees with Claude's "%R ≈ random" finding and sharpens it: once name mix is removed, it is slightly adverse.
- **Multiple comparisons:** C alone is 120 cells (plus 60 matched cells). Rounds B–D add about 130 more. With so many cells, an isolated positive cell would be expected by chance. None appeared anyway, because the null is not "net 0" but "net = −spread".

## B. Cost levers (random entries, all buckets)

### B1. Universe, and the passive-entry bounds

Columns: market = pays the full spread; mid = entry at mid (pays spread/2 on exit); bid = entry at bid (pays 0 net).

| Lever | Hold | spread bp (mean) | market IS \| OOS | mid-entry bound IS \| OOS | bid-entry bound IS \| OOS |
|---|---|---|---|---|---|
| CUR all | 30m | 19.1 / 15.3 | −20.1 \| −16.4 | −10.6 \| −8.7 | −1.0 \| −1.0 |
| CUR all | 60m | 19.3 / 15.5 | −21.3 \| −17.0 | −11.7 \| −9.3 | −2.0 \| −1.6 |
| CUR spr ≤ 10 | 30m | 4.9 | −5.5 \| −5.5 | −3.0 \| −3.1 | −0.6 \| −0.7 |
| CUR spr ≤ 5 | 30m | 3.2 | −3.4 \| −3.6 | −1.8 \| −2.0 | −0.2 \| −0.4 |
| LIQ all | 30m | 6.3 / 5.6 | −6.6 \| −5.6 | −3.4 \| −2.8 | −0.3 \| −0.1 |
| **LIQ ≤ 3** | 30m | 2.2 | **−2.3 \| −2.3** | −1.1 \| −1.2 | −0.0 \| −0.1 |
| **LIQ ≤ 3** | 60m | 2.2 | **−2.3 \| −2.4** | −1.2 \| −1.3 | −0.1 \| −0.2 |
| LIQ ≤ 3 | to 15:50 | 2.2 | −5.1 \| −6.6 | −4.0 \| −5.5 | −2.9 \| −4.4 |

Even the zero-cost (bid-entry) bound is ≤ 0 everywhere, because gross is about 0.

### B2. Passive entry with a fill proxy

The fill proxy: an IEX 1m low trades strictly through the limit within 5 minutes; the exit is at market. The fill rate is only a crude, bar-based estimate: no queue position, and IEX lows miss prints on other venues.

| | Hold | market net | limit at mid: fill rate, net per fill (gross) | limit at bid: fill rate, net per fill (gross) | net per attempt (bid) |
|---|---|---|---|---|---|
| CUR random | 30m | −20.1 \| −16.4 | 68% / 66%, −18.8 \| −15.7 (gross −10.7 \| −9.2) | 54% / 53%, −14.3 \| −11.9 (gross −8.8 \| −7.4) | −7.8 \| −6.3 |
| CUR wr | 30m | −13.8 \| −12.5 | 75% / 72%, −13.0 \| −12.1 | 62% / 61%, −9.2 \| −8.7 | −5.7 \| −5.3 |
| LIQ≤3 random | 30m | −2.3 \| −2.3 | 85% / 83%, −3.4 \| −3.1 | 80% / 78%, −2.8 \| −2.6 | −2.3 \| −2.0 |
| LIQ≤3 wr | 60m | −2.0 \| −2.5 | 86% / 85%, −3.1 \| −3.4 | 82% / 80%, −2.3 \| −2.9 | −1.9 \| −2.3 |

- **Adverse selection:** filled limit orders gross 7–10 bp *worse* than market entries in CUR, and about 2 bp worse in LIQ. A passive entry recovers about 6 bp of the 15–19 bp per fill in CUR and nothing in LIQ. The per-attempt numbers look better only because unfilled attempts count as 0.

## D. Published intraday effects (IEX bars, full SIP spread at entry)

Universe ALL = ~759 Stage-B names + LIQ, prior close ≥ $20, ≥ 150 IEX bars that day. The day-clustered t is given for both halves.

| Effect | n/day | IS net (t) | OOS net (t) | vs its baseline IS \| OOS | pass? |
|---|---|---|---|---|---|
| D1 momentum SPY, long/short by first-30m sign, 10:00→15:50 | 1 | +2.1 (+0.3) | −0.3 (−0.1) | +4.1 \| +1.3 | no |
| D1 QQQ long/short | 1 | −3.6 (−0.3) | −12.2 (−1.5) | +5.4 \| −16.7 | no |
| D1 LIQ list long/short | 96 | −7.8 (−1.7) | −9.4 (−2.6) | +3.8 \| −1.9 | no |
| D1 top-10 gainers long/short | 10 | −26.1 (−0.7) | −16.4 (−0.4) | +7.0 \| −31.4 | no |
| D1 top gainers, long only if first 30m up | 9 | −31.3 (−0.6) | +0.7 (+0.2) | 0 | no |
| D2 ORB 5m long (top-10 gainers) | 7.8 | −36.5 (−1.4) | +50.5 (+0.9) | −39.3 \| +22.5 | no (flips) |
| D2 ORB 5m long+short | 13 | −17.7 (−0.3) | +15.4 (+0.7) | −26.0 \| −2.2 | no |
| D2 ORB 15m long | 7.5 | −34.0 (−1.2) | +24.6 (+0.3) | −43.7 \| +5.1 | no |
| D2 ORB 15m long+short | 11 | −36.8 (−1.4) | +6.1 (+0.4) | −49.2 \| −8.7 | no |
| D2 ORB 30m long | 7 | −34.6 (−1.2) | −0.2 (−0.1) | −36.3 \| −16.0 | no |
| D2 ORB short (5/15/30) | 2–6 | +8.7 / −41.7 / −10.0 | −39.3 / −41.3 / −36.2 | all negative | no |
| D3 gap-up ≥3%, continuation 09:35→15:50 | 22 / 10 | −39.8 (−1.6) | +34.6 (−0.4) | −41.3 \| +51.5 | no |
| D3 gap-up fade 09:35→10:30 | 22 / 10 | +8.5 (+0.1) | −37.2 (−0.2) | +33.2 \| −30.2 | no |
| D3 gap-down ≥3%, continuation →15:50 | 22 / 16 | −62.7 (−0.5) | +51.1 (+0.5) | −39.0 \| +60.5 | no |
| D3 gap-down fade →10:30 | 22 / 16 | +39.0 (−0.5) | −32.1 (−1.2) | +45.1 \| −13.8 | no |

- Every D effect flips sign between halves, and none has |t| > 1 in the right direction in both.
- The gainer, ORB and gap samples are very noisy: pooled means of ±30–70 bp with day-clustered t near 0 (a few days dominate). Where the pooled mean and the day-clustered t disagree in sign, a handful of days carry the mean.
- The SPY/QQQ momentum sample is only 40 + 20 days, which is far too few for a daily-frequency effect.
- The D1 spec here is open→10:00 predicting 10:00→15:50, as asked. The published version (Gao et al.) is prior close→10:00 predicting the **last half hour** (15:30→16:00). That was not tested here.

## Scorecard: every idea against the bar

The bar: net > 0 OOS, beats random, enough trades per day, and a CI that does not span big losses.

| Idea | IS net bp | OOS net bp | vs random (OOS) | trades/day | pass |
|---|---|---|---|---|---|
| Current desk (real fills 9/16–25) | −26.7/trade (mid move −12.7, cost 14.1) | — | worse than random by ~12.7 bp (mid) | ~39 | ✗ |
| CUR random, any hold (5m–15:50) | −19 to −27 | −15 to −22 | = baseline | — | ✗ |
| CUR %R −50 cross, any hold | −14 to −22 | −12 to −19 | −1 to −9 (matched) | ~1,050 signals | ✗ |
| CUR best bucket (10:30–12, 30–60m) | −14.5 to −16.4 | −11.9 to −12.9 | — | — | ✗ |
| CUR spread ≤ 5 bp, 30m | −3.4 | −3.6 | — | ~5.6k grid | ✗ |
| **LIQ≤3 random, 5–60m** | **−2.2 to −2.3** | **−2.2 to −2.4** | = baseline | ~2.5k grid | ✗ (closest to 0) |
| LIQ≤3 %R, 30m | −1.8 | −2.5 | −1.0 (matched) | ~250 | ✗ |
| LIQ≤3 %R, 10:30–12:00, 30m | −0.3 | +0.4 (t +0.5) | +0.3 (matched) | ~63 | ✗ (IS < 0; one of 120 cells) |
| Passive at bid (CUR random, 30m), per fill | −14.3 | −11.9 | +4.5 vs market | ~53% fill | ✗ |
| Passive at bid (LIQ≤3, 30m), per fill | −2.8 | −2.6 | −0.3 vs market | ~78% fill | ✗ |
| D1 intraday momentum (SPY) | +2.1 | −0.3 | +1.3 | 1 | ✗ |
| D1 momentum (LIQ / top gainers) | −7.8 / −26.1 | −9.4 / −16.4 | −1.9 / −31.4 | 96 / 10 | ✗ |
| D2 ORB long 5/15/30 (top gainers) | −36.5 / −34.0 / −34.6 | +50.5 / +24.6 / −0.2 | +22.5 / +5.1 / −16.0 | ~7–8 | ✗ (IS < 0; CI ±100 bp) |
| D3 gap continuation / fade | −40 to +39 | −37 to +51 | flips | 10–22 | ✗ |

## Caveats

- **IEX bars:** prices are the last IEX print, which can be stale in thin names. We require a print ≤ 5 min old at entry and forward-fill exits. This is validated against SIP (corr 0.958, identical mean), but it adds noise to single trades and to D's open and gap prints. The fill proxy uses IEX lows, which miss prints elsewhere and give no queue position.
- **Spread model:** a sym-day median × a time-of-day profile, not the exact quote at each entry. The mean spread in CUR (15–19 bp) is well above the median (8.4 bp) because of a fat tail; results "at the median" would be about 8 bp less negative, still < 0.
- **Universe look-ahead:** CUR and ALL come from names the desk nominated in September, applied to June–September. LIQ is a list of today's large caps (mild survivorship). The CUR drift of −6 to −8 bp to 15:50 may partly reflect this.
- **Sample:** 60 summer days, and OOS is only 20 days. Daily-frequency effects (D1 SPY/QQQ, D3) have 20–60 observations, so they have almost no power.
- **Multiple testing:** about 250 cells in all. No cell passed, so no correction changes the conclusion. Any future "winner" from a grid like this needs a fresh holdout.

## Recommended round 2

1. **Stop paying 15–20 bp to trade about-zero-gross minute moves in $20–100 momentum names.**
   - If the desk keeps trading while research continues, restrict it to LIQ≤3-type names. The same (non-)signal then costs about −2 bp instead of about −16 bp per trade: about −$0.23 instead of −$1.60 per $1,000 round trip.
   - That makes it a cheap testbed, not a profit source.
   - Also, the real fills show that exits within 5 minutes and sub-$20 names carry most of the mid-to-mid loss. The $20 floor already addresses the second.
2. **Look for signal only where the cost hurdle is about 2 bp (LIQ≤3), with a real holdout.** Candidates suggested by this round:
   - **Short-term reversal:** the %R up-cross underperforms matched random by 2–9 bp at 60–120 min. Test a cross-sectional "fade the 15–30 min movers" in LIQ, SPY-hedged, 60–120 min holds.
   - **The proper Gao et al. spec:** prior close→10:00 and 15:30 predicting 15:30→16:00 in SPY/QQQ/sector ETFs. This needs bars to 16:00 and more history (1–2 years of free IEX daily-ish data).
   - **Overnight vs intraday split** in liquid ETFs.
3. **Extend history.** Use 120–250 days of IEX 1m bars for LIQ only (100 names at about 9 s/day is cheap), so that daily effects get real power. Pre-register the one or two specs before running them.
4. **Passive execution** only matters once gross is above about 2 bp. The bar proxy says limit-at-bid saves about 6 bp per fill in CUR but loses 7–10 bp of gross to adverse selection. Measure real paper limit-order fill quality (Alpaca paper fills are optimistic) before counting on it.

## Files

- `tools/studies/edge_a_cost_decomp.py`: part A (reuses `tools/exec_report.py`'s SIP NBBO lookup; cache `ai_reports/edge_nbbo_cache.json`)
- `tools/studies/edge_fetch.py`: IEX bars (`ai_reports/edge_iex_bars/`) and spread samples (`ai_reports/edge_spreads.json`); defines the LIQ list
- `tools/studies/edge_common.py`: loaders, dense-minute view, spread model, day-clustered stats and bootstrap
- `tools/studies/edge_c_grid.py`: parts B and C (`build`, then `report`; records in `ai_reports/edge_c_records.npz`)
- `tools/studies/edge_d_effects.py`: part D

Run on the mini from the repo root: `PYTHONPATH=$PWD:$PWD/tools:$PWD/tools/studies nice -n 15 .venv/bin/python tools/studies/<script>.py`
