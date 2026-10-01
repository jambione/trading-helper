# After the close — 2026-10-01

The intraday desk's working list for after today's close. Jonathan observes the
session and adds items; Claude keeps this file current and adds whatever the
day's checks turn up. Newest items at the bottom of each section.

## Jonathan's observations

- [ ] **Today's first 6 opens vs TradingView (Jonathan's charts, 10:21-10:37): data now matches.** Logged %R vs IEX rebuild within ~2-6 pts on all 6; squares were real (OXY red box, SNDQ pinned). 6 trades -$5.16. Losers: SNDQ at RSI2 99.7 on the top bar of a spike (out in 24 s), RKLB #2 at RSI2 97.7 at its high, XENE pre-square at RSI2 34 while fading off its 10:18 high. Tested 10:50 on 531 fills 9/1-10/1 (/tmp/two_patterns.py on the mini): (1) RSI2 > 95 DEAD - -0.31% vs ~-0.20%, t -1.2, no gradient (80-90 worst -0.40%, 90-95 best +0.04%). (2) 'falling' entries (last 5m down or >=0.5% under the 10-min high) PROMISING BUT UNPROVEN - pre-square -0.32% (n=11) vs -0.08% (n=42); full square -0.45% (n=15) vs -0.15% (n=93); t -1.3; seat class logged only since mid-Sept so no first-half check. No live change; re-run in 1-2 weeks.
- [x] Mobile: let the Positions/Overnight section collapse so the book gets more room (shipped ede4bed).

## From today's checks (Claude)

- [ ] Engine FLY fix (681c933) first live day: compare logged %R with IEX-rebuilt %R at every fill (`pctr_check.py` in the scratchpad / `/tmp/pctr_check.py` on the mini).
- [x] Overnight first scored night (9/30 -> 10/1): all 20 at the crosses +25.1 bp ($+50 at $1k/name) vs backtest 16; paper +$19 on the 10 filled (+26 bp). VICR +14% carried it; 13 of 20 names were down. All 10 paper sells filled.
- [ ] Overnight: first -1% filtered buy (15:58), every kept name filled.

## Deploy after the close (needs a desk restart)

- [ ] **Restart the trader after 16:00** so ai_trader publishes `overnight_live` (cec8751) and the dashboard's LIVE line can appear. Static files and overnight_book.py are already live; this is the only piece waiting.

## Overnight LIVE test (Monday 10/5)

- [x] Live mode built, off by default (21300d7), sizing fix (b98b8af), compare report + dashboard LIVE line (cec8751).
- [x] Live keys in, read-only check passed: account ACTIVE, $100 cash, multiplier 1 (cash account), separate from desk and paper. Dry run would buy ERAS, IOVA, RLAY x1 (~$47.68) by MOC.
- [ ] 15:40 filter validation (running).
- [x] Live agent INSTALLED, DISARMED (f09725e, 11:29): every order step logs 'LIVE not armed'.
- [ ] Mon 10/5 before 15:40: `touch ~/repo/trading-helper/config/overnight_live.armed` to arm (rm to disarm).

## Shipped before the open (watch today)

- [x] Engine %R counts minutes, not IEX rows (`rte_minute_grid`, default on). FLY 9/30 12:16 now reads -38 / -75 vs TradingView -33 / -71 (was -39 / -40). Shipped pre-open with Jonathan's OK. Watch: squares should now match the chart; arms may shift on thin names.
- [x] Overnight paper P&L matched first-in-first-out (leftover shares keep their own buy price).

## Found in today's data check (09:47)

- [x] **Engine %R can leave its -100..0 range** (MNKD +21.7/+9.4 = fake full square; AI -160.9). `_check_proximity` injects the live price as the last bar's close without widening its high/low; on the Alpaca fallback the last bar can be minutes old. Fixed and shipped mid-session at Jonathan's call (6b4b50b, engine restarted 09:54).
- [ ] Minor: dashboard.log has ~22k `socket.send() raised exception` warnings (closed browser tabs) — log noise; NLST rt age reads 5.4 years (bad trade timestamp).

## Blockers, 09:30-10:00 (0 fills; 765 arm checks, 38 names)

- [x] **spread_unknown root cause (traced 10:20):** the async gate warmer is one serial thread calling all 5 input fns per name. vol_now (added 9/30, ttl 30s) is now its biggest load at 37.5 fetches/min; spread fetches fell 20.2 -> 14.7/min vs 9/30; day_high (gate OFF) still costs 18/min. Plus ~32 names where the SIP spread fetch itself returned None (no quotes in the delayed minute, or an errored/429 request cached as None for 180s). Shipped 10:12 (109546c) at Jonathan's call. Result 10:15-10:25: spread_unknown 33% -> 5% of checks; warmer fetches ~111 -> ~15/min. Remaining 5% = spread fetch genuinely empty (no quotes in the delayed minute, or an errored request cached as None for 180s) - smaller follow-up.
- [ ] (earlier note) **spread_unknown is 32% of refusals.** The spread gate reads SIP, served 16 min late: every name is unknown until 09:46, and each newly added name waits ~16 min. Spread_wide is another 23%. Options: IEX-quote fallback before SIP is served, or a pre-open seeded spread. Needs a decision.
- [ ] **tape_only 23%:** thin names with no fresh print (EFXT, MNKD, KURA, GLOB, CMCT).
- [x] **SNXX 0.0% — traced, NOT a data bug.** SNXX faded from +1.58% (09:39) to a real print at $16.74 = yesterday's close at 09:43:38 (live, 1.6s old), then -0.15%, then back to +0.5%. The `not_uptrend` drop (roster row, live desk quote) was correct. My first read (stale price) came from 1-minute bar closes hiding the tick.
- [ ] Design question (Jonathan): the uptrend re-gate uses the instantaneous day change, so a name chopping around flat can be dropped on one tick and re-qualify a second later. Smooth it (e.g. below flat for 30-60s)?
- Note: the 09:54 mid-session restart cost ~52 checks of spread_unknown while caches refilled.

## No opens through 10:20 (yesterday: 7-10 by then)

- [ ] Squares were MORE common today (300 overbought checks vs 222 on 9/30) but only 3 armed (vs 35). 74% of today's squares died on spread_wide (87), spread_unknown (69), tape_only (65); yesterday ~33%. Today's book is thin names (KORU, EFXT, GLOB, NOWL, CNXC, TDAY) and only 7-15 names. spread_unknown doubled vs 9/30 (161 -> 296) = the warmer slowdown, fixed 10:12. Question for Jonathan: is the 0.2% spread gate right on thin-name days, or is the issue which names reach the book?

- [ ] **More names on the book?** Cap is 12 seats (book server live); held 7-8, all movers. Admission refused 1,709 times on 87 names since the open: spread_wide 644, no_tape/stale_tape 608, float>800M 189, not_uptrend 100, gapped_down 88. After close: replay today with seats 12->20, spread gate 0.2/0.3/0.4%, float cap lifted / bigger movers list, and show opens AND gross/net side by side (memory: full book = opens, not profit; spread gate cut cost 0.112% -> 0.071%).

- [ ] **Seat held by a name that can't trade:** SDEV was refused spread_wide 65 times in 15 min (10:12-10:27) while holding one of the 12 seats. Should a seat be released after N consecutive spread_wide refusals? (Opens resumed after the 10:12 warmer fix: XENE 10:21, OXY 10:23, SNDQ 10:23.)

- [ ] **Positions open already down (Jonathan: LYTE -$0.70 at open).** LYTE: IEX quote at decision 25.44/25.445, fill 25.46875 x16, marked ~25.425. Two parts: (1) the spread (buy at ask, marked at bid), (2) IEX's quote was not the real market - fill came 2.4c above the IEX ask (memory: fills land at the SIP NBBO touch, 0 excess). Known cost ~10 bp round trip, ~half the desk's loss 9/23-9/29. Test after close: mid-price / bid-resting entry limits on recent entries with historical SIP quotes - fill rate within 10-30 s AND outcome of filled vs unfilled (adverse selection).

## Open engineering items

- [x] Item 3: overnight paper P&L first-in-first-out (d8b1c8b, live).
- [x] Item 4: engine %R in minutes (d2630f1, live pre-open).
- [ ] Item 5: the 7 tests that were already failing. Running in a separate session (task_6be76155).
- [x] Item 6: replay tests engine changes. `--engine recompute` (26ebb1d) for indicator math; `--engine rt` (e575c9f) rebuilds the engine's bar store from recorded prints. Verified on the 9/30 FLY window: pre-fix code reproduces the fake square and buys FLY (~12:17:40, live 12:18:10); fixed code never squares.

## Context

- 9/30 replay: 55 opens, -1.4 bp/trade gross; costs ~10 bp a trade on top (spread).
- Every entry/exit/filter variant tested since August scores ~0 gross (see memory: exits-cannot-move-the-mean, entry-setups-are-noise-equivalent, the-universe-fades-at-every-horizon). Ideas worth trying are ones that change *which names* or *what information* the desk uses, not another threshold.
