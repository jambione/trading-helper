# After the close — 2026-10-01

The intraday desk's working list for after today's close. Jonathan observes the
session and adds items; Claude keeps this file current and adds whatever the
day's checks turn up. Newest items at the bottom of each section.

## Jonathan's observations

- [x] Mobile: let the Positions/Overnight section collapse so the book gets more room (shipped ede4bed).

## From today's checks (Claude)

- [ ] Engine FLY fix (681c933) first live day: compare logged %R with IEX-rebuilt %R at every fill (`pctr_check.py` in the scratchpad / `/tmp/pctr_check.py` on the mini).
- [x] Overnight first scored night (9/30 -> 10/1): all 20 at the crosses +25.1 bp ($+50 at $1k/name) vs backtest 16; paper +$19 on the 10 filled (+26 bp). VICR +14% carried it; 13 of 20 names were down. All 10 paper sells filled.
- [ ] Overnight: first -1% filtered buy (15:58), every kept name filled.

## Shipped before the open (watch today)

- [x] Engine %R counts minutes, not IEX rows (`rte_minute_grid`, default on). FLY 9/30 12:16 now reads -38 / -75 vs TradingView -33 / -71 (was -39 / -40). Shipped pre-open with Jonathan's OK. Watch: squares should now match the chart; arms may shift on thin names.
- [x] Overnight paper P&L matched first-in-first-out (leftover shares keep their own buy price).

## Found in today's data check (09:47)

- [x] **Engine %R can leave its -100..0 range** (MNKD +21.7/+9.4 = fake full square; AI -160.9). `_check_proximity` injects the live price as the last bar's close without widening its high/low; on the Alpaca fallback the last bar can be minutes old. Fixed and shipped mid-session at Jonathan's call (6b4b50b, engine restarted 09:54).
- [ ] Minor: dashboard.log has ~22k `socket.send() raised exception` warnings (closed browser tabs) — log noise; NLST rt age reads 5.4 years (bad trade timestamp).

## Blockers, 09:30-10:00 (0 fills; 765 arm checks, 38 names)

- [ ] **spread_unknown is 32% of refusals.** The spread gate reads SIP, served 16 min late: every name is unknown until 09:46, and each newly added name waits ~16 min. Spread_wide is another 23%. Options: IEX-quote fallback before SIP is served, or a pre-open seeded spread. Needs a decision.
- [ ] **tape_only 23%:** thin names with no fresh print (EFXT, MNKD, KURA, GLOB, CMCT).
- [ ] **SNXX armed 09:41:41, then dropped at 09:43:40 `not_uptrend` with pct_change exactly 0.0** on a movers-list name. Looks like a missing day change read as 0, not a real flat day. Check the movers admission path.
- Note: the 09:54 mid-session restart cost ~52 checks of spread_unknown while caches refilled.

## Open engineering items

- [x] Item 3: overnight paper P&L first-in-first-out (d8b1c8b, live).
- [x] Item 4: engine %R in minutes (d2630f1, live pre-open).
- [ ] Item 5: the 7 tests that were already failing. Running in a separate session (task_6be76155).
- [x] Item 6: replay tests engine changes. `--engine recompute` (26ebb1d) for indicator math; `--engine rt` (e575c9f) rebuilds the engine's bar store from recorded prints. Verified on the 9/30 FLY window: pre-fix code reproduces the fake square and buys FLY (~12:17:40, live 12:18:10); fixed code never squares.

## Context

- 9/30 replay: 55 opens, -1.4 bp/trade gross; costs ~10 bp a trade on top (spread).
- Every entry/exit/filter variant tested since August scores ~0 gross (see memory: exits-cannot-move-the-mean, entry-setups-are-noise-equivalent, the-universe-fades-at-every-horizon). Ideas worth trying are ones that change *which names* or *what information* the desk uses, not another threshold.
