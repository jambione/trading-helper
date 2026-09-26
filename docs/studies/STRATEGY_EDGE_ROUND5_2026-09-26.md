# Strategy edge — Round 5 (T0–T7), 2026-09-26

**Status:**
- The full round-5 spec (T1–T4) arrived at 14:40 ET. It was pre-registered in `docs/studies/edge5b_prereg.json` (14:44 ET, before any T1–T4 result). The earlier note saying T1–T4 were unspecified is superseded.
- Order run: T4 → T1 → T3 → T2 → T5. Sections for T4, T1, T3 and T2 follow the T5 section below. T0, T6 and T7 come first.

Pre-registrations: `docs/studies/edge5_prereg.json` (T0/T5/T6/T7, 14:36 ET) and `docs/studies/edge5b_prereg.json` (T1–T4 + costs/splits, 14:44 ET), both before any result.
Scripts (uncommitted): `tools/studies/edge5_fetch.py`, `edge5_study.py`, `edge5_t4.py`, `edge5_t1.py`, `edge5_t1b.py`, `edge5_t3.py`, `edge5_t2.py`.
Rules as before: free data only, read-only, nice 15, ≤1 request/s. The round-4 S1 quote fetch is paused while these fetches run.

## T0 — Data inventory (from grepping the repo; no secrets read)

| Source | Where | Used for | Measured as a signal? |
|---|---|---|---|
| Alpaca bars (IEX live; SIP history) | 79 StockBarsRequest sites: ai_entry_watch, realtime_bars, tools/* | live bars, all studies | yes (rounds 1–4) |
| Alpaca latest quote / trade / snapshot | ai_entry_watch, alpaca_price_poll, dashboard | live price, freshness | plumbing only |
| Alpaca historical quotes (SIP) | exec_report, universe_screen, edge studies | spread model | spread only; NBBO imbalance = round-4 S1 (pending) |
| Alpaca trades | flow_monitor/trades.py, tools/feed_gaps.py | tape window, gap audit | **no** |
| Alpaca screener (movers, most actives) | movers_screener, swing_screener, flow_monitor/screener | Movers seed | yes (T7 below) |
| Alpaca news (Benzinga) | news_feed.py (live cache, observe), tools/catalyst_screen.py | catalyst features, retro screen | intraday screen only (no cost); **daily hold untested** until T5 |
| Alpaca options (indicative) | none | — | **unused**. Historical option bars are served (SPY 2024 contract: 77 daily bars; 2026: 32). IV/skew is a future test |
| Alpaca corporate actions | none | — | unused |
| Finnhub | stock/profile (float_feed), quote, websocket trades (finnhub_stream) | share count, real-time prints | float measured; **company-news endpoint unused** |
| Massive.com | massive_client.py (EOD bars, news with sentiment) | referenced by realtime_bars/signal_engine | unmeasured (key presence not checked) |
| Stocktwits | trending_screener, stocktwits_trending, seed_rank | Trending seed | yes (T7) |
| Finviz | finviz_universe, rs_screener | universe / RS screen | not in this series |
| yfinance | dashboard.py | display | no |
| nasdaqtrader symbol list | transcription/ticker_extract | ticker recognition | n/a |
| Discord (OCR reader) | discord_source, open_discord_alerts; logged in sessions/*/discord.jsonl.gz (9/25+) | seed | T7: 62 rows, none with usable bars; **unmeasured** |
| Research seeds (Claude/anthropic, xAI/Grok, agy) | seed_rank, ai_suggest, _manual_*research_run; ai_reports/claude_research_*.md (193 files from 8/02), seed_rank_raw (9/07+) | Research seed | yes (T7) |
| bb_live, momentum (internal scanners) | tapes source labels | seeds | yes (T7) |
| TradingView | agent_bus / desk_hotkeys / charts | operator charts | no |

**Unused or unmeasured:** Alpaca options, corporate actions, trades-as-signal, Finnhub company news, Massive news sentiment, Discord (not enough logged history).

## T7 — Do the desk's seed sources beat random liquid names? (descriptive; no split)

Setup:
- Nominations are the first appearance per (source, symbol, day) in `ai_reports/tapes/*` shadow/rejects plus the `sessions/*/sources` stream: 6,193 rows, 2026-08-06..09-25, 35 days.
- Entry is the close of the nomination day. Nominations after 15:55 are skipped.
- Excess is measured against the equal-weight mean of the 108 liquid U2 names over the same horizon.
- Returns are gross (no cost). t is day-clustered.

| Source (all prices) | 1 day: n, mean excess (t) | 5 days | 10 days |
|---|---|---|---|
| momentum | 2,792, **−110 bp** (−3.7) | 2,156, **−448** (−7.1) | 1,705, **−712** (−6.4) |
| movers | 657, **−160** (−2.4) | 422, **−608** (−3.7) | 225, **−622** (−3.1) |
| bb_live | 349, **−264** (−2.3) | 287, **−868** (−4.7) | 226, **−1,168** (−5.5) |
| trending | 1,185, +8 (+0.3) | 915, +105 (+0.1; median +20) | 632, −5 (−0.7) |
| research (Claude/xAI/agy) | 514, +6 (+0.7) | 442, −72 (−0.1) | 344, −150 (−0.7) |
| all sources | 5,497, −89 (−3.8) | 4,222, −333 (−5.8) | 3,132, −534 (−7.3) |

In the desk's $20–100 band:
- momentum: −78 / −245 / −588 bp (t −2.2 / −2.0 / −2.7)
- trending: +9 / +94 / −101 (t < 1.4)
- research: +37 / −19 / −21 (t +1.3 / 0 / −0.4)
- movers: n 155 / 23 / 15, uninformative

**Reading:**
- No source beats random liquid names at 1, 5 or 10 days.
- Momentum, movers and bb_live picks keep falling after they are nominated: −4% to −12% relative over 5–10 days, with t of −3 to −7, and most so in the sub-$20 names.
- Trending and Research are indistinguishable from random.
- Held overnight, a hot-mover nomination is a fade signal, not a buy signal. Trading that as a short needs borrow, SSR rules and wide spreads. That is a separate question and was not tested.

## T6 — Breadth / regime filter (book leg; the T1-picks leg is in the T1 section)

Pre-registered rule, as of the prior close: % of U2 names above their 50-day SMA ≥ 50% AND SPY 20-day realized vol ≤ its trailing 250-day median.
- The rule is ON for 62/150 tune, 21/50 validate and 17/50 holdout days.
- It is applied to the round-4 L intraday book: max 5 concurrent, $1,000/position.

| Book | Part | All days: bp/trade, $/day | Filter ON | Filter OFF |
|---|---|---|---|---|
| limit 10bp, 5m, 30 min | tune | −3.0, −15.92 | −3.8, −19.78 | −2.4, −13.19 |
| | validate | −3.6, −19.72 | −3.1, −17.49 | −3.9, −21.34 |
| | holdout | −1.8, −10.04 | −2.2, −11.94 | −1.7, −9.07 |
| market 30 min | tune | −6.6, −38.67 | −5.6, −32.95 | −7.3, −42.69 |
| | validate | −5.4, −32.02 | −5.7, −34.12 | −5.2, −30.50 |
| | holdout | −4.3, −25.34 | −5.9, −35.51 | −3.4, −20.11 |

**Reading:** the regime filter does not help. ON days are no better than OFF days in any consistent way, and both lose.

Book figures differ from the round-4 table by about 0.1–0.2 bp. The cause is the tie-break shuffle, which is seeded with Python's per-process string hash. It does not affect any conclusion; a fixed seed would remove it.

## T5 — News catalyst (Alpaca news, 2025-08 → 2026-09)

- 55,533 news items fetched for U2, giving 39,801 symbol-days with news. The news window is 16:00 on the prior day to 15:30.
- Events:
  - **E1:** news + a large move + high volume.
  - **E0:** the same move and volume with **no** news.
  - **E2:** a news-count spike.
- Trades follow the day's direction (long or short), entering at the close. Excess is measured vs the U2 equal-weight basket, net of cost.
- Tune and validate come from the ~14-month window. Holdout is run only for promoted cells, and nothing was promoted.

| Event, side | 1d tune / validate (t) | 5d tune / validate | 10d tune / validate |
|---|---|---|---|
| E1 news+move+vol, long (n 215/60) | −14 (0.0) / +73 (1.5) | −5 / −79 | +19 / −345 |
| E1, short (226/50) | −2 / +20 | −14 / +40 | −29 / −70 |
| E0 move+vol, no news, long (27/10) | +39 / +159 | +15 / −172 | +26 / −192 |
| E0, short (38/11) | −42 / −135 | −42 / −596 | +64 / −767 |
| E2 news spike, long (424/134) | −28 (−1.2) / −26 | −39 / +21 | +1 / −45 |
| E2, short (404/113) | −2 / +54 (2.0) | +8 / +75 | −13 / +69 |

**T5 verdict:**
- **No news-catalyst edge, and nothing was promoted.** Every event type flips sign between tune and validate.
- A news-backed move is no better than the same move without news.

## Options

The free indicative options feed serves historical daily bars for SPY contracts (2024 and 2026 tested). An implied-vol / skew regime test is feasible later from those bars. It is not tested here.

## T4 — Mark / Get set / Go (required)

**Setup** (`edge5_t4.py`; output `ai_reports/edge5/t4.json`, variant 2 in `t4_v2.json`)
- Universe: U2, the 108 liquid names. SIP 1-minute bars over the edge2 250-day split: tune 150 / validate 50 / holdout 50 (2025-09 → 2026-09-25). Daily history is used for the Mark.
- **Mark** (known before the open): top decile (11 names) by 60-day return, tie-break 20-day, only when SPY closed above its 50-day SMA the prior day. Mark days: tune 108/150, validate 49/50, holdout 40/50.
- **Get set:** the first 1-minute bar closing between 10:00 and 11:00 with price above session VWAP AND return since 09:30 above SPY's.
- **Go (two variants):** (a) %R(14, 1-min) crosses up through −50 before 11:30, entry at the next 1-minute open; (b) a limit 10 bp below the Get-set price fills within 10 minutes, filled at the limit.
- **Mark-only** buys the 09:45 open. **Mark+Set** buys the open of the minute after Get set.
- Holds: 30 min, 60 min, 15:50, next close (1d), 5 days. One trade per name per day.
- Cost: full modeled round-trip spread, plus 2 bp for any overnight or open-print leg.
- "vs random": 5 unmarked U2 names bought at the same minute with the same hold and cost. "−SPY" is gross minus SPY over the same interval. t is date-clustered.

**Variant 1 (spec Mark).** Net bp per trade, with t and excess vs random in brackets, for tune | validate | holdout:

| Stage (trades/day T/V/H) | 30 min | 60 min | 15:50 | 1 day | 5 days |
|---|---|---|---|---|---|
| Mark-only (7.9/10.8/8.8) | −25.1 (t −5.5) [−9.8] \| +3.4 (0.3) [+13.9] \| −25.0 (−3.2) [−8.9] | −27.8 [−13.6] \| −16.9 [−10.2] \| −23.2 [−11.6] | −26.4 [−8.5] \| −7.6 [+9.0] \| −19.9 [−6.3] | +1.6 [+19.1] \| +8.2 [+19.2] \| −28.4 [−11.5] | +105 (3.3) [+123] \| +73 (0.8) [+78] \| −82 (−1.8) [−70] |
| Mark+Set (5.0/7.2/5.7) | −19.8 (−4.6) [−10.0] \| −7.4 [−0.9] \| −16.3 (−2.8) [−9.4] | −22.9 [−13.6] \| −16.5 [−13.9] \| −14.7 [−9.2] | −18.7 [−6.0] \| −5.0 [+1.5] \| −11.5 [−3.5] | +19.1 [+28.6] \| +44.5 [+40.8] \| −25.5 [−18.0] | +122 (3.5) [+137] \| +50 (0.8) [+44] \| −111 (−2.0) [−109] |
| +Go %R (5.0/7.1/5.7) | −17.3 (−5.9) [−9.1] \| −14.8 [−7.3] \| −12.9 (−2.7) [−6.6] | −19.5 [−8.8] \| −16.3 [−7.6] \| −7.9 [−0.3] | −13.7 [−0.2] \| −7.1 [+1.1] \| −5.0 [+1.9] | +24.8 [+41.7] \| +46.0 [+47.7] \| −19.2 [−5.3] | +129 (3.6) [+153] \| +47 (0.8) [+52] \| −109 (−2.0) [−73] |
| +Go pullback (4.0/6.0/4.5) | −14.8 (−3.2) [−10.5] \| −2.5 [+3.8] \| −7.4 [−3.6] | −18.8 [−13.8] \| −12.9 [−9.1] \| −4.7 [−3.7] | −20.8 [−11.2] \| −10.2 [−4.9] \| −3.5 [−0.1] | +17.8 [+32.2] \| +55.9 [+52.2] \| −20.0 [−12.9] | +142 (3.7) [+166] \| +14 (0.9) [+8] \| −107 (−1.9) [−100] |

Counts: Mark-only n = 1188 / 539 / 440. Mark+Set n = 752 / 360 / 286. Go %R n = 748 / 357 / 284. Go pullback n = 605 / 302 / 227. The full per-cell table (CI, random mean, −SPY with t) is in `_raw.txt`.

**Variant 2 (Mark = T1's tune+validate leader: top 5 by 12-1 month momentum, no regime filter, all 250 days).**
- Intraday is still negative. Mark-only 30m: −14 / −13 / −41. 15:50: −20 / −11 / −8.
- Multi-day is positive in all three parts but with weak t.
  - Mark-only 1d: +10 / +35 / +22, excess vs random +26 / +43 / +35, t ≤ 1.1.
  - Mark-only 5d: +150 / +206 / +107, excess +169 / +221 / +88, t 3.6 / 1.6 / 0.7. The 5-day holds overlap, so these t values are overstated.
  - Set and Go add nothing consistent. Go %R 1d: excess +30 / +95 / +95, t 2.2 / 1.2 / 1.2. Go pullback 5d holdout: net 0.
- The T4 window (2025-09 → 2026-09) lies inside T1's holdout period. Variant 2 is therefore not an independent confirmation of T1.

**T4 verdict:**
- **As a same-day desk strategy (flat by 15:50), there is no edge.** Every stage and every intraday hold is negative in essentially every split, and none beats random names bought at the same minute.
- Get set and Go reduce the loss a little by trading less and later. They do not create positive expectancy.
- The only positive numbers come from holding overnight or for days, and those come from the Mark (a momentum selection), not from the Set/Go timing.
- With the spec Mark (60-day, SPY > SMA50), even the 5-day hold fails its holdout (−82 to −111 bp, t ≈ −2).

## T1 — Horizon ladder on daily bars

**Setup** (`edge5_t1.py` + `edge5_t1b.py`; `ai_reports/edge5/t1.json`)
- Data: U2 SIP daily bars, split+dividend adjusted, 2016-01 → 2026-09-25. Signal dates run 2017-01-04 → 2026-09-24.
- Split: tune to 2022-10-28, validate to 2024-10-10, holdout to 2026-09-24.
- Signals:
  - RS20 / RS60 / RS12_1: top decile, then top N.
  - NEAR_HIGH: within 2% of the 252-day high.
  - PULLBACK: close > SMA50 and (RSI(2) < 10 or 3 down closes).
- Grid: regime none / SPY > SMA50 / SPY > SMA200; entry next open or signal-day close; hold 1/2/5/10/20 days to the close; N = 5 or 10. That is 300 cells.
- Cost: symbol modeled round-trip spread + 2 bp.
- Baseline: the equal-weight U2 basket, which is the expected value of random picks on the same dates at the same cost.
- **Survivorship: the universe is today's 108 liquid names applied back to 2017.** This flatters momentum (names that became mega-caps are in; names that fell out are missing). No broader point-in-time liquid set was available free, so none was run.

**Pre-registered promotion rule** (excess > 0 on tune and validate, combined date-clustered t ≥ 2): 56 of 300 cells pass. But nearly all passes are 10–20-day holds, where the date-clustered t counts overlapping holds as independent and so is heavily inflated.
- I re-tested the leaders overlap-free: rebalance every h days and average the t over all h offsets.
- Holdout was run once, for the top 5 cells only.

| Cell | Part | Basket net bp/trade | Excess vs random | −SPY | Clustered t (overlapping) | Overlap-free t |
|---|---|---|---|---|---|---|
| RS12_1 top5, none, next-open, 20d | tune | +212 | +83 | +123 | 5.8 | 1.29 |
| | validate | +305 | +119 | +134 | 3.9 | 0.88 |
| | holdout | +454 | +315 | +331 | 6.7 | 1.50 |
| RS12_1 top5, none, close, 20d | T/V/H | +227 / +319 / +482 | +92 / +128 / +337 | +134 / +143 / +353 | 6.4 / 4.2 / 7.0 | 1.43 / 0.93 / 1.57 |
| RS60 top5, SPY>SMA200, open, 20d | T/V/H | +201 / +222 / +378 | +92 / +61 / +268 | +127 / +69 / +300 | 6.2 / 2.5 / 6.2 | 1.37 / 0.59 / 1.39 |
| RS12_1 top5, none, open, 10d | T/V/H | +97 / +157 / +207 | +40 / +65 / +146 | +59 / +73 / +152 | 3.8 / 3.0 / — | 1.20 / 0.94 / 1.39 |
| RS12_1 top5, none, open, 5d | T/V/H | +37 / +65 / +97 | +15 / +24 / +73 | +24 / +28 / +75 | — | 0.83 / 0.72 / 1.34 |
| RS12_1 top5, none, open, 1d | T/V/H | −9.6 / −2.2 / −2.9 | −4.2 / −0.2 / +1.6 | — | — | −1.36 / −0.04 / 0.19 |

The daily-portfolio rate (1/h of capital entering each day) is basket net ÷ h. For RS12_1 top5 20d that is ≈ +11 / +15 / +23 bp/day gross of financing; excess vs random is ≈ +4 / +6 / +16 bp/day.

Summary at regime none, next-open entry, N = 10 (net bp/trade, excess vs random in brackets), tune | validate:

| Signal | 1d | 2d | 5d | 10d | 20d |
|---|---|---|---|---|---|
| RS20 | −5 [+0] \| +2 [+4] | +2 [+0] \| +16 [+7] | +21 [−1] \| +56 [+14] | +58 [+1] \| +111 [+19] | +127 [−2] \| +202 [+16] |
| RS60 | −8 [−3] \| −1 [+1] | +2 [−0] \| +10 [+2] | +28 [+6] \| +37 [−5] | +76 [+19] \| +72 [−21] | +174 [+44] \| +162 [−23] |
| RS12_1 | −8 [−3] \| −5 [−3] | +2 [+1] \| +8 [−1] | +30 [+8] \| +45 [+3] | +77 [+20] \| +113 [+21] | +165 [+35] \| +232 [+47] |
| NEAR_HIGH | −7 [+1] \| −2 [+1] | −2 [+1] \| +1 [−4] | +14 [+4] \| +20 [−14] | +43 [+6] \| +52 [−24] | +86 [+2] \| +111 [−55] |
| PULLBACK | −3 [+2] \| +0 [+2] | −4 [−1] \| +17 [+4] | +10 [−2] \| +44 [+2] | +21 [−18] \| +88 [+7] | +61 [−25] \| +149 [−26] |

**Robustness of the leader (RS12_1 top 5, 20d):**
- Excess by year: 2017 +132, 2018 0, 2019 +56, 2020 +315, 2021 −159, 2022 +82, 2023 +87, 2024 +342, 2025 +121, 2026 +498 (bp per 20-day trade). Positive in 8 of 10 years.
- Concentrated: TSLA/COP/NVDA drive tune, NVDA/PLTR/META drive validate, MU/PLTR/LRCX drive holdout.
- Overlap-free t is about 0.9–1.6 per part, about 2.1 pooled.

**T6 regime rule on T1 picks:** it does not help. RS12_1 top5 20d excess is +19 / +129 / +349 when ON versus +139 / +107 / +288 when OFF. The OFF days are as good or better.

**T1 verdict:**
- **1–2-day holds: nothing.** Every signal is at or below random after costs.
- **10–20-day holds: 12-1 month momentum (top 5) beats random picks in every split and in 8 of 10 years**, by about +80 to +300 bp per 20-day trade.
- It passes the pre-registered rule only because of overlap-inflated t. Overlap-free, no single split is significant (t ≈ 1–1.6). It is concentrated in a handful of mega-winners, and the universe is survivorship-biased in its favor.
- This is the textbook momentum factor: plausible, but weak and slow. It is not a day-trading edge.
- NEAR_HIGH and PULLBACK fail validation. Regime filters do not improve RS12_1.

## T3 — Calendar effects on SPY / QQQ (2021-09-27 → 2026-09-25, 1255 sessions)

- Split: tune to 2024-09-24, validate to 2025-09-25, holdout to 2026-09-25.
- Cost: 2.3 bp per trade (0.3 bp spread + 2 bp open/overnight leg).
- "Excess" is the effect's gross minus the all-sessions mean over the same number of sessions. FOMC dates were hardcoded from the public calendar (41 statements).

SPY, net bp per trade (t), tune | validate | holdout:

| Effect | n (T/V/H) | Tune | Validate | Holdout | Excess vs random days T/V/H |
|---|---|---|---|---|---|
| Turn of month (close before last day → close of day +3) | 36/12/12 | −6.8 (−0.2) | +5.7 (0.1) | +95.2 (2.0) | −23 / −20 / +69 |
| Pre-holiday session | 29/11/10 | +20.5 (1.5) | +8.9 (0.5) | +10.7 (0.7) | +18 / +4 / +6 |
| FOMC day (close-close) | 24/8/8 | +30.1 (1.0) | −19.9 (−0.5) | −51.5 (−1.8) | +28 / −25 / −56 |
| Monday | 138/48/48 | +4.5 | +6.9 | +31.5 (3.0) | +2 / +2 / +27 |
| Tuesday | 156/52/52 | −0.3 | +3.1 | −3.2 | −3 / −1 / −8 |
| Wednesday | 155/51/52 | +0.2 | +30.0 (1.3) | +6.8 | −2 / +25 / +2 |
| Thursday | 152/50/49 | +3.2 | −15.3 | −8.8 | +1 / −20 / −14 |
| Friday | 152/50/50 | +4.2 | −2.2 | −1.0 | +2 / −7 / −6 |
| Overnight (close→open), gross | all | +2.0 (0.9) | +2.8 (0.6) | +6.5 (1.9) | net after 2.3: −0.3 / +0.6 / +4.3 |
| Intraday (open→close), gross | all | +2.6 (0.8) | +4.1 (0.6) | +0.6 (0.2) | |

QQQ has the same pattern with larger amplitude: pre-holiday +25 / +4 / +17 net, TOM −9 / +33 / +110, FOMC +62 / −9 / −35, Monday +7 / +10 / +49.

**T3 verdict: no calendar effect is usable.**
- Nothing is positive with t ≥ 2 on both tune and validate.
- Pre-holiday is the only effect positive in all three parts. It is small (t 0.5–1.5) and occurs about 10 times a year.
- Turn of month and Monday worked only in the most recent year. FOMC day flipped sign.
- Overnight is weakly larger than intraday in the holdout only.

## T2 — Earnings drift

**Data limits:**
- Finnhub free `/stock/earnings` returns only the **last 4 quarters** per symbol, with no report date. `/calendar/earnings` returns nothing for past windows on the free tier.
- Reaction days were therefore **inferred** from daily bars: the stock's volume ÷ its 20-day median, relative to SPY's, ≥ 2.5; greedy by size with ≥ 40 sessions between events; U2 single stocks only (90 names, ETFs excluded).
- This found 2,412 events over 10.6 years, 2.5 per symbol-year. About 60% of actual reports are caught; quieter reports are missed.
- Split by event date: tune to 2022-07-12, validate to 2024-07-30, holdout to 2026-09-23.
- **Signal A:** reaction-day return minus SPY in the top quintile, threshold ≥ +427 bp, fixed on tune.

| Signal A | Entry | Hold | Tune n / net / excess (t) | Validate | Holdout |
|---|---|---|---|---|---|
| top-quintile reaction | next open | 1d | 290 / +5 / +8 (0.0) | 117 / −24 / −5 | 112 / −14 / −12 |
| | next open | 5d | +49 / +9 (0.2) | +35 / +3 | −41 / −67 (−1.2) |
| | next open | 10d | +65 / −4 | +124 / +16 | +45 / −14 |
| | next open | 20d | +113 / −2 | +282 / +65 (1.1) | +198 / +101 (1.2) |
| | reaction close | 1d | +5 / −1 | −30 / −11 | +12 / +4 |
| | reaction close | 5d | +48 / −0 | +30 / −2 | −16 / −52 |
| | reaction close | 20d | +113 / −9 | +278 / +61 (1.0) | +226 / +117 (1.3) |

- (Info) Bottom-quintile reactions: 1d excess −28 (t −2.3) in tune, but +7 in validate and +11 in holdout. No robust continuation on the downside either.
- (Info) Across all reactions, excess is ≈ 0 at every hold.

**Signal B (EPS surprise > 0; Finnhub, 90 symbols × 4 quarters):**
- 152 of 360 rows matched an inferred reaction day, all of them in the holdout period. B is **UNDERPOWERED** (about 1 year, n ≈ 116) and has no tune or validate split.
- Next-open entry, excess vs random: 1d +1, 5d −27, 10d −28, 20d −88 (all |t| < 1).
- Surprise ≤ 0 (n = 36): 5d excess −93 (t −1.3).
- A + B combined (n = 35): 20d excess +1.

**T2 verdict: no earnings drift in these liquid large caps.**
- Top-quintile reactions do not drift in tune (excess −2 to −9 bp at 20d) and are only weakly positive in validate and holdout (t ≈ 1). This fails the rule.
- The EPS-surprise leg is too short to judge, and what exists is flat or negative.
- A real test needs point-in-time report dates, which require a paid calendar or scraping.

## Round-5 summary

| Test | Best pre-registered cell | Tune / validate / holdout (net bp, excess vs random) | Verdict |
|---|---|---|---|
| T4 Mark/Set/Go, same day | Go-pullback 15:50 | −20.8 / −10.2 / −3.5; excess −11 / −5 / 0 | **No edge.** Every intraday stage × hold loses; Set/Go ≈ random. |
| T4, multi-day (spec Mark) | Mark+Set 5d | +122 / +50 / −111 | **Fails holdout.** |
| T4 variant 2 (RS12_1 Mark) | Mark-only 5d | +150 / +206 / +107; excess +169 / +221 / +88 | Positive, but t 3.6 / 1.6 / 0.7 with overlapping holds, and the window lies inside T1's holdout. Supportive only. |
| T1 horizon ladder | RS12_1 top 5, 20d, next open | +212 / +305 / +454; excess +83 / +119 / +315 | **Weak positive.** Consistent sign in all splits and 8/10 years, but overlap-free t ≈ 1.3 / 0.9 / 1.5, survivorship-biased and concentrated. 1–2d holds: nothing. |
| T6 regime on T1 picks | — | ON days not better than OFF | **No help.** |
| T3 calendar SPY/QQQ | Pre-holiday | +20.5 / +8.9 / +10.7 (t ≤ 1.5) | **Nothing usable.** |
| T2 earnings drift | Top-quintile reaction, 20d | excess −2 / +65 / +101 | **Fails tune.** EPS leg underpowered. |
| T5 news catalyst | — | signs flip | **No edge.** |
| T7 seed sources (earlier) | — | hot-mover nominations fade | **No source beats random.** |

**Single best candidate: 12-1 month momentum, top 5 of the liquid 108, held about 20 trading days.**
It is the only thing in rounds 1–5 that beats random picks in every split. It is a slow swing/factor trade, not a day trade, and its statistical support is modest: overlap-free t ≈ 2 pooled, before accounting for survivorship.

**What a paper trial would look like (not implemented; needs Jonathan's decision):**
- **Selection (pre-open, from prior closes):** rank U2 by close[t−21] / close[t−252] − 1 and take the top 5. No regime filter; T6 and SPY-SMA filters did not help.
- **Execution:**
  - Stagger entries: buy 1/20 of the sleeve each day, one tranche per day, so 20 overlapping tranches.
  - Alternatively, run a single sleeve rebalanced every 20 sessions.
  - Enter at the open, or at the signal-day close (same result within ~15 bp).
  - Use marketable limits in these liquid names; modeled cost is about 3–4 bp per round trip.
- **Size:** a fixed paper sleeve (e.g. $5k = 5 × $1k per tranche set). There are no stops in the tested rule.
- **Duration and success bar:** at least 6 months (about 6 non-overlapping periods is still underpowered). Judge on excess vs the equal-weight U2 basket and vs SPY, not on raw P&L. Pre-commit a kill rule, e.g. stop if cumulative excess < −5% after 3 months.
- **Desk rule changes needed:**
  1. Allow **multi-day/overnight holds** for this sleeve only. It must be exempt from the 15:50 flatten and from max-hold/intraday time stops.
  2. Exempt it from intraday entry gates (the %R arm, VWAP, the 10:00–11:30 window, max-concurrent limits of the day book), or run it as a separate book or account tag.
  3. Add a daily pre-open job that computes the ranking from daily bars.
  4. Ring-fence its capital from the day-trading book, and account for its P&L separately.
  5. Accept earnings and gap risk overnight; decide whether to skip names reporting within the hold.
- **Keep the day-trading desk as is (or paused):** none of rounds 1–5 found a same-day edge that survives costs.

**Caveats:**
- Survivorship (today's U2 back to 2017) inflates momentum.
- The t for overlapping holds is overstated in the pre-registered clustered stat. The overlap-free t is reported alongside.
- Holdout use: T1 holdout was run once, for the top 5 cells. T4 variants each ran once over all splits (the spec requires reporting holdout). T3 and T2 holdout columns were each run once. T5 had no promotions, so its holdout was not run.
- Book results carry ±0.2 bp from the hash-seeded shuffle.
- Finnhub free has only 4 quarters, so earnings days are inferred and miss about 40% of reports.
