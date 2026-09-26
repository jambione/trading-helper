# Strategy edge — Round 4: volume, spread and pullback-entry signals on free historical SIP (2026-09-26)

**Verdict up front: no volume, spread or pullback signal clears costs.**
- 0 of about 430 long cells passed the pre-registered promotion rule. That covers V1–V3 in both universes (280), test P in both universes (144) and S1 (pending). So no holdout was spent on promotions.
- The pullback-after-arm idea (test P, the headline) is negative per arm in every L cell except five with 1–8% fill rates. It is negative in every C cell.
- The dips it buys behave like random dips, with no bounce.
- Paying for SIP is **not** justified by this evidence. SIP-detected volume signals carry only about 1–2 bp more gross than IEX-detected ones, which is below the spread in every universe.

Pre-registration (written before any fetch or result):
- `docs/studies/edge4_prereg.json` (13:10 ET)
- test-P addendum `docs/studies/edge4_prereg_P.json` (13:26 ET)

Scripts (uncommitted):
- `tools/studies/edge4_fetch.py`, `edge4_study.py`, `edge4_pullback.py`, `edge4_report.py`

Raw tables: `docs/studies/STRATEGY_EDGE_ROUND4_VOLUME_SPREAD_2026-09-26_raw.txt`.

Data rules:
- Free data only. SIP 1m bars and quotes come from Alpaca's historical endpoint.
- **Verified first:** SIP bars for 9/25 and 2025-10-01 came back. A request ending 5 minutes ago was refused ("subscription does not permit querying recent SIP data"). Historical SIP works; live SIP does not, so nothing live changes.
- Read-only, nice 15, ≤1 multi-symbol request/s, no config/restart/commit/push, no pytest.

Universes and costs (the same as rounds 1–2):
- **L** = the U2 liquid list (108 names), 250 days, edge2 split: tune 150 (2025-09-29..2026-05-04) / validate 50 / holdout 50 (2026-07-17..09-25).
- **C** = Claude's Stage-B $20–100 universe (about 250 names/day), 60 days (2026-06-22..09-15): tune 40 / holdout 20.
- Cost = the full modeled SIP quoted spread at entry (round-1/2 SpreadModel). Modeled spreads average about 5 bp in L (0.3 bp SPY up to 18 bp BLK) and about 25–30 bp for C at signal times.
- **"L tight"** is a post-hoc info cut, not pre-registered: the 43 L names with model median spread ≤3 bp (the ETFs, AAPL, MSFT, NVDA, AMZN, GOOGL, TSLA, BAC, …).
- t = day-clustered: the mean of day means over its standard error. When trades cluster on a few days, per-trade net and t can disagree in sign. That happens for V1 pace ≥3 below.

---

## 1. Headline — test P: buy the dip after the %R −50 arm (L universe)

**Design** (Jonathan's idea):
- The live arm fires. That is fast %R(21, EWM 7) crossing up through −50 with slow %R(112, EWM 3) rising, computed on IEX 1m bars as the desk sees them, one arm per name per 15 min, 09:45–15:30.
- A buy limit rests at arm price − d. The arm price is the SIP close of the arm bar.
  - d = 5 / 10 / 20 / 40 bp, or 0.25 / 0.5 × the 5-min ATR.
  - The limit waits W = 2 / 5 / 10 min, then is cancelled.
- **Fill rule:** it fills only if a SIP 1m low trades strictly through the limit. The fill price is the limit, so there is no entry spread. Half the spread is paid on exit.
- **Holds:** 15 / 30 / 60 min, or to 15:50, measured from the fill.
- **Comparisons:**
  - Market entry on the same arms: full spread, same hold measured from the arm.
  - A random-time limit with the same d, W and hold: same name, same clock hour, another day in the same split part, 3 draws.

L: about 159k arms over 250 days (about 640/day across 108 names). Tune+validate pooled, bp:

| cell (d, W, hold) | fill | net per ARM (t) | net per FILL | gross per fill | market net, same arms | market gross: filled arms / unfilled arms | random limit: fill, gross per fill, per attempt |
|---|---|---|---|---|---|---|---|
| 5bp, 5m, 30 | 63% | −2.0 (−6.8) | −3.2 | −0.3 | −6.0 | **−5.3 / +9.0** | 62%, −0.4, −2.0 |
| 10bp, 5m, 30 | 39% | −1.3 (−5.5) | −3.2 | −0.1 | −6.0 | **−10.2 / +6.5** | 38%, −0.5, −1.3 |
| 20bp, 5m, 30 | 16% | −0.4 (−2.9) | −2.5 | +0.8 | −6.0 | **−19.2 / +3.6** | 15%, −0.4, −0.6 |
| 0.25 ATR, 5m, 30 | 63% | −1.9 (−7.5) | −3.1 | −0.3 | −6.0 | −6.6 / +11.5 | 62%, −0.6, −2.1 |
| 0.5 ATR, 5m, 30 | 36% | −1.0 (−6.6) | −2.8 | −0.1 | −6.0 | −12.0 / +6.9 | 36%, −0.4, −1.1 |
| 0.5 ATR, 2m, 60 | 17% | −0.4 (−3.0) | −2.5 | +0.2 | −6.1 | −11.7 / +2.4 | 16%, −0.8, −0.6 |
| 40bp, 10m, to 15:50 (best per arm) | 8% | +0.1 (+0.2) | +1.6 | +4.2 | −7.1 | −35.9 / +1.8 | 8%, −0.3, −0.2 |

Results across all 72 L cells:
- **Per arm:** negative in 67. The 5 positive cells are +0.04 to +0.12 bp per arm, all at 1–8% fill (40 bp dips, 20bp:2:eod), with t < 1.
- **Promotions:** 0.
- **Vs random limits:** no cell beats the random-time limit (|t_excess| < 2 everywhere).
- **Gross per fill:** median −0.2 bp, against −0.5 bp for random-time limit fills.
- **L tight (43 names, info):** the same picture. Per arm is −0.3 to −1.8 for the active cells. Per fill is about −1 bp net and ~0 gross. Market on the same arms is −1.9 bp. The 40 bp cells are +0.1 per arm at 2–5% fill.

**C (60 days, 64k arms, tune 40 days):**
- All 72 cells are negative per arm (best −0.8).
- Per fill gross is −0.6 to −2 bp at 30 min and −8 to −30 bp to 15:50.
- Market on the same arms is −13.8 bp (30 min).
- Arm dips come out about 0.9 bp better gross than random dips (median). Only one cell has excess t ≥ 2 (0.5 ATR, 2m, 30: per arm −1.2), which is what ~72 tries produce by chance.

**Do dips after an arm keep falling or recover?** Neither. They behave like any random dip in that name and hour:
1. **Strong adverse selection relative to the arm.** Arms that dip d bp within W minutes are the losers: market-from-arm gross is −5 to −36 bp on the filled arms, against mostly positive (about +2 to +12 bp in L) on the arms that never dipped. Filled arms are worse than unfilled ones in every cell, by 9 to 43 bp in L. The limit order systematically skips the arms that work and buys the ones that don't. This is the same mechanism as round 1's limit-at-bid result.
2. **After the fill, price roughly flatlines.** Gross from the fill price is about 0 (−0.3 to +0.8 at 30 min in L). That is what a random-time limit fill does too (−0.4 to −0.6). The dip neither continues nor bounces more than chance.
3. **Why per arm looks "less bad" than market entry:** it trades 16–63% of the time and pays half a spread instead of a full one. It is not a better entry, just fewer and cheaper losing trades.

### Book simulation (L)

Rules: max 5 concurrent positions, one per name, arms 09:45–15:30, exits by 15:50, $1,000 per position.
- A limit takes a slot only when it fills. A fill that finds the book full or the name already open is dropped.
- Cells: the 2 best by tune+validate net per arm with a meaningful count, one high-activity cell (10bp:5:30), and market entry.
- **Caveat:** these cells were picked after seeing tune+validate. The book's holdout is the first look at those 50 days for these cells.

| spec | part | opens / 10 min | ≥1 open | ≥2 open | net bp/trade (t) | $/day @ $1k | days + |
|---|---|---|---|---|---|---|---|
| limit 40bp, 10m, to 15:50 | tune | 0.14 | 98% | 97% | +3.6 (+0.4) | +1.77 | 78/150 |
| | validate | 0.14 | 98% | 98% | −4.0 (−0.2) | −1.98 | 26/50 |
| | holdout | 0.14 | 98% | 97% | +4.8 (+0.4) | +2.39 | 25/50 |
| limit 20bp, 2m, to 15:50 | tune | 0.13 | 99% | 97% | +3.4 (+0.4) | +1.65 | 73/150 |
| | validate | 0.14 | 99% | 99% | −10.0 (−0.5) | −5.00 | 23/50 |
| | holdout | 0.14 | 99% | 98% | −9.4 (−0.9) | −4.71 | 22/50 |
| limit 10bp, 5m, 30 min (active) | tune | 1.47 | 98% | 96% | −3.1 (−4.2) | −16.74 | 61/150 |
| | validate | 1.52 | 99% | 97% | −3.6 (−3.1) | −19.91 | 17/50 |
| | holdout | 1.50 | 99% | 97% | −2.1 (−2.3) | −11.63 | 20/50 |
| market, 30 min | tune | 1.60 | 99% | 98% | −6.5 (−11.9) | −37.91 | 24/150 |
| | validate | 1.62 | 100% | 99% | −6.1 (−5.2) | −36.18 | 13/50 |
| | holdout | 1.62 | 100% | 99% | −4.4 (−5.5) | −25.81 | 9/50 |
| market, to 15:50 | tune / val / hold | 0.14 | 100% | 100% | −11.8 / −29.9 / −20.4 | −5.90 / −14.97 / −10.22 | |

L tight (43 names ≤3 bp, info), tune / validate / holdout:
- **limit 10bp:5:30:** 1.15–1.24 opens/10 min, ≥1 open 95–97%, ≥2 open 85–90%. Net −0.1 / −2.4 / +0.9 bp. $/day −0.47 / −10.92 / +3.83.
- **market 30:** 1.5 opens/10 min. Net −1.9 / −2.2 / −1.5 bp. $/day −10.24 / −11.97 / −8.18.
- **limit 40bp:10:eod:** net +2.1 / −9.2 / +3.6. $/day +0.95 / −4.61 / +1.61.
- **limit 20bp:2:eod:** net +0.5 / +0.6 / −2.5. $/day +0.20 / +0.30 / −1.05.

How to read the book:
- The "to 15:50" cells fill all 5 slots with the first fills of the morning and hold them all day, about 5 trades/day. That is not the "open many times all day" design, and their sign flips between parts. The flips are noise at t < 1.
- The high-activity limit book (about 1.5 opens per 10 min, ≥2 open 96%+ of the session) loses about half as much as market entry but still loses in every part of the full L list. On the tight names it is about flat (−0.1 / −2.4 / +0.9 bp): cheaper than market (−1.5 to −2.2), but not positive.
- **Occupancy is not the problem.** The book is essentially always ≥2 open. The average trade has no edge.

---

## 2. V1–V3 volume signals (long; SIP- and IEX-detected; outcomes on SIP prices)

Claude's prior numbers (tools/studies, 9/23–9/26), reused as definitions:
- **Volume pace `rvol_pace`**: SIP volume so far / (20-day average × expected fraction). `runway_target_study`: pace ≥ 1.64 raised the runway rate (+2% before −1% in 60 min) from 4.0% to 16.0% (z +13.8). The name-quality rerun had the 1.64–2.5× bucket at 65% runners, +0.12 / +0.23% mean at 30/60 min (train) and +0.23 / +0.46% (test). As a book filter it gave S7 +0.046%/trade (t ~0.9).
- **Volume breakout `vol_brk`**: 1m volume > 3× its 20-min mean and close > the prior 15-min high (`wr_trigger_study`, 8 days, SIP). Runway 1.22×, fwd15 +9.4 bp gross (t 3.1), halves +3 / +12. Detected on IEX, fwd15 fell to +2.0 (t 0.6) with halves −10 / +9. Best combo D (%R cross + volume > 2× + 15-min high) was +12.5 fwd15 on SIP and ~0 on IEX. The source-optimal paper book with vol_brk was negative net for every source (momentum spreads 23–34 bp).

This round, pre-registered:
- **V1:** cumulative volume ≥ 1.6 / 2 / 3 × the same-time-of-day 20-day average, and price > VWAP and > prior close, on a 15-min grid.
- **V2:** 1m or 5m volume ≥ 3 / 5 × the trailing 20-bar mean, and a new 15 / 30-min high.
- **V3 VSA:** effort + result, absorption, no-supply pullback, entry at the signal bar close.
- Holds 15 / 30 / 60 / 120 min and to 15:50. The 09:30–09:45 window is excluded.

**Results — L (140 long cells), C (140 long cells):**
- **Promotions:** 0. **Cells with t ≥ 2:** 0 of 280. **Cells with t ≤ −2:** 138 (L) and 140 (C).
- **Short mirrors:** none at t ≥ 2.
- **Gross:** +0 to +3 bp in L for most cells, below the modeled spread (about 5 bp average; L-tight about 2.5 bp).
- **L-tight (info):** net is −0.3 to −6 bp; 0 of 140 cells at t ≥ 2.
- **C:** gross ≤ 0 almost everywhere; net −20 to −50 bp.
- **Baselines:** the random same-name, same-hour baseline nets about −8 bp in L and −25 to −30 bp in C. Excess over random is within ±3 bp, mostly t < 2.
- **V1 pace ≥3 at 120 min / to 15:50 (L):** +20.5 / +13.8 bp gross per trade and +9.9 / +3.4 net, but day-clustered t is −2.5. These signals bunch on a few high-volume market days, which carry the per-trade mean, while most signal days lose. Validate alone at 120 min: −11.7 bp (SIP).

**SIP vs IEX volume (long, L, tune+validate pooled, gross bp per trade, 30-min hold):**

| signal | SIP-detected: n, gross, net | IEX-detected: n, gross, net |
|---|---|---|
| V1 pace ≥1.6 | 16,115, +0.7, −6.7 | 22,260, +0.8, −6.6 |
| V1 pace ≥2 | 7,126, +0.5, −8.4 | 10,876, +1.3, −6.9 |
| V1 pace ≥3 | 1,976, +3.9, −6.5 | 2,640, +4.1, −6.6 |
| V2 1m 3× hi15 (Claude's vol_brk) | 25,654, +0.8, −7.2 | 54,929, −0.0, −8.7 |
| V2 1m 5× hi30 | 6,964, +1.9, −5.7 | 17,333, +0.0, −9.4 |
| V2 5m 3× hi30 | 3,086, +2.2, −5.2 | 5,684, +0.2, −8.8 |
| V2 5m 5× hi30 | 904, +3.0, −4.5 | 1,536, +1.3, −8.2 |
| V3 EFR | 60,798, +0.1, −7.9 | 127,196, +0.1, −7.6 |
| V3 NS | 86,987, +0.4, −7.7 | 57,346, +0.4, −4.7 |

What the comparison shows:
- **SIP volume matters for the breakout (V2), not for pace (V1) or VSA (V3).** SIP-detected breakouts carry about 1–2 bp more gross than IEX-detected ones and fire 2–3× less often, because IEX volume spikes are noisier. On L-tight the same gap holds, e.g. V2 1m 5× hi30 at 30 min: SIP +2.3 gross / −0.3 net vs IEX +0.6 / −1.8.
- **This agrees in direction with Claude's 8-day result** (SIP +9.4 vs IEX +2.0 fwd15), but the size over 200 days is about a fifth.
- **Even the best SIP-detected cell stays negative net** on L-tight at the tightest spreads (≤3 bp).
- In C, SIP vs IEX makes no difference: both gross ≤ 0.

---

## 3. S1 spread / NBBO-imbalance signals (SIP quotes) — PENDING

Design, pre-registered:
- The first 30 LIQUID names, sampled on 59 days (tune[::6], validate[::3], holdout[::3]).
- One NBBO snapshot per minute, 09:45–15:50, from the last quote in (t−1 s, t]. Entry is one minute later.
- **S1a:** spread ≤ 0.67 × its trailing 30-snapshot median.
- **S1b:** bid size / (bid + ask) ≥ 0.75 (3×) or ≥ 0.833 (5×).

**Status:**
- The quote fetch is about 21,600 requests at ≤1/s, roughly 6.3 h. It is still running on the mini (`/tmp/e4_quotes.out`, `ai_reports/edge4/quotes/`), interleaved across tune/validate/holdout so any prefix is balanced. Coverage is about 78% of name-minutes (no quote update inside the 1-second window for the rest).
- The pipeline has been smoke-tested on the first 3 days. Those numbers are not reported because 2 days per part are too few for day-clustered t.
- **To finish:**
  `ssh mac-mini-away 'cd ~/repo/trading-helper && export PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies; nice -n 15 .venv/bin/python /tmp/edge4_study.py S1 && .venv/bin/python /tmp/edge4_report.py S1'`
  Scripts are also in tools/studies/.
- **Prior expectation:** the top-30 names are mostly 1-tick-spread ETFs and mega caps. S1a can only fire on multi-tick names, and both signals would need more than about 1–3 bp of gross.

---

## 4. Multiple testing and caveats

- **Multiple testing:** about 430 long cells, heavily correlated. By chance alone, several would have been promoted. None were, and the distribution is shifted negative (t ≤ −2 in 278 of 280 V cells). So there is no "near miss" to rescue.
- **Fill model for test P is optimistic on price and pessimistic on count.** A fill requires a SIP low strictly through the limit, with no queue position. Real limit fills would have more adverse selection, not less.
- **Spread model:** the round-1/2 SpreadModel (samples every 3rd day plus a time-of-day profile). Tight-name results use the same model.
- **The V1 "same-time-of-day 20-day average"** uses minute-summed volume from the same feed, so numerator and denominator come from one feed and one granularity, per Claude's rule.
- **Holdout** was not consulted for any V/S/P cell because none were promoted. It was consulted only in the requested book simulation of cells chosen on tune+validate.

## 5. Plain verdict

- **Does any volume or spread signal clear costs?** No. Not in the liquid list, not in its ≤3 bp subset, and not in Claude's $20–100 universe.
- **Test P (dip after arm):** a fill selects the losers among arms, and bought dips behave like random dips. It loses less than market entry only because it trades less and pays half a spread.
- **Would paying for SIP be justified?** No. SIP volume improves breakout detection by about 1–2 bp gross, a real but small effect that agrees with Claude's direction. It is not enough to turn any tested signal positive after spread, and every tested signal is at or below zero gross in the universe the desk actually trades (C). Revisit only if a strategy shows positive net on IEX detection first.
