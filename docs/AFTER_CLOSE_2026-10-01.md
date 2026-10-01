# After the close — 2026-10-01

The intraday desk's working list for after today's close. Jonathan observes the
session and adds items; Claude keeps this file current and adds whatever the
day's checks turn up. Newest items at the bottom of each section.

## Jonathan's observations

- [ ] **Today's first 6 opens vs TradingView (Jonathan's charts, 10:21-10:37): data now matches.** Logged %R vs IEX rebuild within ~2-6 pts on all 6; squares were real (OXY red box, SNDQ pinned). 6 trades -$5.16. Losers: SNDQ at RSI2 99.7 on the top bar of a spike (out in 24 s), RKLB #2 at RSI2 97.7 at its high, XENE pre-square at RSI2 34 while fading off its 10:18 high. Tested 10:50 on 531 fills 9/1-10/1 (/tmp/two_patterns.py on the mini): (1) RSI2 > 95 DEAD - -0.31% vs ~-0.20%, t -1.2, no gradient (80-90 worst -0.40%, 90-95 best +0.04%). (2) 'falling' entries (last 5m down or >=0.5% under the 10-min high) PROMISING BUT UNPROVEN - pre-square -0.32% (n=11) vs -0.08% (n=42); full square -0.45% (n=15) vs -0.15% (n=93); t -1.3; seat class logged only since mid-Sept so no first-half check. No live change; re-run in 1-2 weeks.
- [x] Mobile: let the Positions/Overnight section collapse so the book gets more room (shipped ede4bed).

## From today's checks (Claude)

- [x] (10/1: 1 of 73 fills off >25 pts - XENE 10:21, half-filled slow window; 9/30 was 4 of 36) Engine FLY fix (681c933) first live day: compare logged %R with IEX-rebuilt %R at every fill (`pctr_check.py` in the scratchpad / `/tmp/pctr_check.py` on the mini).
- [x] Overnight first scored night (9/30 -> 10/1): all 20 at the crosses +25.1 bp ($+50 at $1k/name) vs backtest 16; paper +$19 on the 10 filled (+26 bp). VICR +14% carried it; 13 of 20 names were down. All 10 paper sells filled.
- [x] Overnight: first -1% filtered buy (15:58): kept 16/20 (dropped SLS -6.5, VICR -6.7, IOVA -3.1, TWST -2.6), ALL 16 FILLED, top-up had nothing left.

## Deploy after the close (needs a desk restart)

- [x] **Restart the trader after 16:00** (done 16:1x with the a53e4a4 deploy; agy_auth=ok) so ai_trader publishes `overnight_live` (cec8751) and the dashboard's LIVE line can appear. Static files and overnight_book.py are already live; this is the only piece waiting.

## Overnight LIVE test (Monday 10/5)

- [x] Live mode built, off by default (21300d7), sizing fix (b98b8af), compare report + dashboard LIVE line (cec8751).
- [x] Live keys in, read-only check passed: account ACTIVE, $100 cash, multiplier 1 (cash account), separate from desk and paper. Dry run would buy ERAS, IOVA, RLAY x1 (~$47.68) by MOC.
- [x] 15:40 filter validation: holds. OOS drop<-1% +22.2 bp/night at 15:40 vs +21.3 at 15:55 (all 20 +16.5). Auction buy stays at 15:40.
- [x] Live agent INSTALLED, DISARMED (f09725e, 11:29): every order step logs 'LIVE not armed'.
- [x] Self-arming set: the mini's `config/overnight_live.armed` holds `2026-10-05` (6992606), so Monday's 15:40 MOC goes in on its own. Cancel: `rm` the file. Arm now instead: empty it.

## Shipped before the open (watch today)

- [x] Engine %R counts minutes, not IEX rows (`rte_minute_grid`, default on). FLY 9/30 12:16 now reads -38 / -75 vs TradingView -33 / -71 (was -39 / -40). Shipped pre-open with Jonathan's OK. Watch: squares should now match the chart; arms may shift on thin names.
- [x] Overnight paper P&L matched first-in-first-out (leftover shares keep their own buy price).

## Found in today's data check (09:47)

- [x] **Engine %R can leave its -100..0 range** (MNKD +21.7/+9.4 = fake full square; AI -160.9). `_check_proximity` injects the live price as the last bar's close without widening its high/low; on the Alpaca fallback the last bar can be minutes old. Fixed and shipped mid-session at Jonathan's call (6b4b50b, engine restarted 09:54).
- [ ] Minor: dashboard.log has ~22k `socket.send() raised exception` warnings (closed browser tabs) — log noise; NLST rt age reads 5.4 years (bad trade timestamp).

## Blockers, 09:30-10:00 (0 fills; 765 arm checks, 38 names)

- [x] **spread_unknown root cause (traced 10:20):** the async gate warmer is one serial thread calling all 5 input fns per name. vol_now (added 9/30, ttl 30s) is now its biggest load at 37.5 fetches/min; spread fetches fell 20.2 -> 14.7/min vs 9/30; day_high (gate OFF) still costs 18/min. Plus ~32 names where the SIP spread fetch itself returned None (no quotes in the delayed minute, or an errored/429 request cached as None for 180s). Shipped 10:12 (109546c) at Jonathan's call. Result 10:15-10:25: spread_unknown 33% -> 5% of checks; warmer fetches ~111 -> ~15/min. Remaining 5% = spread fetch genuinely empty (no quotes in the delayed minute, or an errored request cached as None for 180s) - smaller follow-up.
- [x] (CLOSED 10/1 eve, Jonathan: keep refusing on unknown - those names were mostly wide, median 0.31%, and lost -0.81% at 15m. Small follow-up only: ~5% empty/errored SIP fetches cached as None.) (earlier note) **spread_unknown is 32% of refusals.** The spread gate reads SIP, served 16 min late: every name is unknown until 09:46, and each newly added name waits ~16 min. Spread_wide is another 23%. Options: IEX-quote fallback before SIP is served, or a pre-open seeded spread. Needs a decision.
- [ ] **tape_only 23%:** thin names with no fresh print (EFXT, MNKD, KURA, GLOB, CMCT).
- [x] **SNXX 0.0% — traced, NOT a data bug.** SNXX faded from +1.58% (09:39) to a real print at $16.74 = yesterday's close at 09:43:38 (live, 1.6s old), then -0.15%, then back to +0.5%. The `not_uptrend` drop (roster row, live desk quote) was correct. My first read (stale price) came from 1-minute bar closes hiding the tick.
- [x] (SHIPPED 10/1 eve: ai_watch_uptrend_grace_sec 60 - a name up within the last 60 s survives a flat tick) Design question (Jonathan): the uptrend re-gate uses the instantaneous day change, so a name chopping around flat can be dropped on one tick and re-qualify a second later. Smooth it (e.g. below flat for 30-60s)?
- Note: the 09:54 mid-session restart cost ~52 checks of spread_unknown while caches refilled.

## No opens through 10:20 (yesterday: 7-10 by then)

- [x] (CLOSED 10/1 eve, Jonathan: keep the 0.2% spread gate - refused squares lost -0.93% at 15m vs -0.15% armed.) Squares were MORE common today (300 overbought checks vs 222 on 9/30) but only 3 armed (vs 35). 74% of today's squares died on spread_wide (87), spread_unknown (69), tape_only (65); yesterday ~33%. Today's book is thin names (KORU, EFXT, GLOB, NOWL, CNXC, TDAY) and only 7-15 names. spread_unknown doubled vs 9/30 (161 -> 296) = the warmer slowdown, fixed 10:12. Question for Jonathan: is the 0.2% spread gate right on thin-name days, or is the issue which names reach the book?

- [ ] **More names on the book?** Cap is 12 seats (book server live); held 7-8, all movers. Admission refused 1,709 times on 87 names since the open: spread_wide 644, no_tape/stale_tape 608, float>800M 189, not_uptrend 100, gapped_down 88. After close: replay today with seats 12->20, spread gate 0.2/0.3/0.4%, float cap lifted / bigger movers list, and show opens AND gross/net side by side (memory: full book = opens, not profit; spread gate cut cost 0.112% -> 0.071%).

- [x] **(SHIPPED 10/1 eve: ai_watch_spread_wide_evict_sec 120) Seat held by a name that can't trade:** SDEV was refused spread_wide 65 times in 15 min (10:12-10:27) while holding one of the 12 seats. Should a seat be released after N consecutive spread_wide refusals? (Opens resumed after the 10:12 warmer fix: XENE 10:21, OXY 10:23, SNDQ 10:23.)

- [ ] **Positions open already down (Jonathan: LYTE -$0.70 at open).** LYTE: IEX quote at decision 25.44/25.445, fill 25.46875 x16, marked ~25.425. Two parts: (1) the spread (buy at ask, marked at bid), (2) IEX's quote was not the real market - fill came 2.4c above the IEX ask (memory: fills land at the SIP NBBO touch, 0 excess). Known cost ~10 bp round trip, ~half the desk's loss 9/23-9/29. Test after close: mid-price / bid-resting entry limits on recent entries with historical SIP quotes - fill rate within 10-30 s AND outcome of filled vs unfilled (adverse selection).

## Tonight's build (Jonathan, 11:40)

- [x] **Three-arm entry test on paper (BUILT + ON, a53e4a4/2d71fca, live from 10/2 open):** rotate entries across (1) control: market at the ask, (2) limit at the mid rounded DOWN to the cent (= bid on 1c spreads), (3) limit at the bid; arms 2-3 cross to the ask after 10 s if unfilled. Log arm, limit price, fill time, filled-passive vs crossed, entry vs SIP mid, P&L. Report after 1-2 weeks. Deploy after the close.

## Today's losses (Jonathan, 13:45) — test tonight on the ~871 fills

37 trades 10:21-13:44, net -$10.88. 9 losers at -0.45..-0.74% = -$21.62 (2x the net); winners top out ~+0.6%, avg ~+0.4%. No single entry feature (square type, top-of-range, TradingView indicators) separates them.
- [x] **Re-entry cooldown after a loss — NOT PROVEN (13:55, /tmp/loss_patterns.py on the mini, 625 fills 9/1-10/1 and 1,189 all-time).** Re-entries within 5 min of a losing exit: -0.35% vs -0.21% (t -1.6; all-time -0.31 vs -0.20, t -1.0), worse in both halves, but no gradient: 3 min t -1.0, 7 min t +0.2, 10 min better than average. Blocking them saves ~$57 only because every trade loses on average. Side finding: re-entries after a WIN beat average (30 min: -0.05% vs -0.23%, t +2.9 all-time, both halves) — names that just worked keep working, still not profitable.
- [x] **Morning window — today was a one-off.** 10:15-11:00 = -0.23% vs -0.22% rest (t 0.0; all-time t -0.6); per day, window minus rest averages -0.02% over 32 days. Every hour loses; afternoon (14:00+) is least bad (-0.11%, t +2.5 all-time, both halves). No live change.
- Note: <$10 names (TDAY, COHH) were 4 trades, -$6.29 (58% of net) — matches cheap-names memory; no $10 minimum (Jonathan's call).

- [x] **Do opens have timing + runway? (14:20, tools/runway_study.py --since 2026-09-15, 489 fills vs 8,582 same-name minutes after first fill.)** Runway exists (median +0.31% up within 15m, 19% reach +1%) but the drop is bigger (median -0.45%). Timing is slightly WORSE than a random minute in the same name: +0.5% before -0.5% 41% vs 48% (~2.8 sigma; part is the ~5 bp half-spread, fills scored from the ask); 30m return -0.48% vs -0.24%. Mean up_15 +0.45pp (t 3.65) but median -0.02pp = a few volatile outliers. What predicts runway (big day change, high vol, cheap, wide spread) also predicts the worse 30m return = volatility, not direction.

- [x] **Pullback entry test (Jonathan, 14:30) - FAILS (16:30, /tmp/pullback.py on the mini, 610 fills 9/1-10/1).** At the signal: +0.5 first 42%, r30 -0.46%. Dip 0.2/0.3/0.5% within 5m: r30 -0.50/-0.57/-0.89% on the trades taken; skips 18/32/56%, and the SKIPPED trades were the winners (r30 +0.07..+0.18% at the signal). Paired on the same trades the dip entry is +0.11..+0.25% better (t ~2) but that is the cheaper price (0.08-0.17% below the signal, and the sim gives the limit no spread), not better timing; +0.5-first only 44% vs 42%. Today: SNDQ/XENE/RKLB/LYTE/AXTI/TDAY all dipped and were still bought; NCLH/ZETA/DRAM/NVTS winners never dipped; WULF +1.08% -> -0.38%. Losers dip and keep going; winners don't come back. Original idea: Today's opens scored trade by trade (/tmp/today_runway.py on the mini): 7 of 9 big losers never traded >0.15% above the fill in 15 min (XENE -0.03, SNDQ -0.39, RKLB#2 -0.15, LYTE -0.17/0.00, AXTI 12:43 -0.01, COHH +0.15) = bought the top tick; up-first 10 of 28 (morning 3/11, afternoon 7/6). Winners ran +0.8..+2.5% (SNXX, WULF, EFXT) and kept 25-40%. Test: instead of entering on the square, wait for the first pullback after it (e.g. first 1m close below the prior close / -0.2..-0.5% off the post-square high, within N min, skip if none) and enter there. Score on 30 days of fills vs same-name random minutes (runway_study method): up-first +/-0.5%, ret 15/30, both halves, how many entries are skipped, and what happens to today's losers and winners.

- [x] **Hold until +X% profit, no stop, else sell 15:55 (Jonathan, 14:40) - FAILS.** 563 fills 9/1-9/30 (/tmp/hold_to_target.py on the mini). Desk as traded -0.24%, 31% win. +0.25%: 85% win, mean -0.25%; +0.5%: 74%, -0.31%; +1%: 59%, -0.29%; +2%: 37%, -0.40%; +5%: 14%, -0.47%. Win rate soars, mean never improves: the misses average -1.8..-3.0% at the close and the worst 5% average -7..-17%. Real collapses held through: RETO 9/16 $10.67 -> $2.03 (-81%, 6 fills), DCOY 9/22 -59%, IMCC 9/18 -52%. No live change.

- [x] **Target + stop grid - FOLLOW-UP DONE, FAILS at the bid (/tmp/tgt_stop_bid.py, 207 fills 9/23-9/30, 1 s SIP prints, stops sold at the SIP bid): +0.5/-0.5 -15.0 bp, +1/-0.5 -17.0, +1/-1 -24.1, +2/-0.5 -22.7, +2/-1 -29.8, +2/-2 -33.4 vs live trail -13.0. Every shape worse than the live trail once the exit pays the spread (some 429s on quotes fell back to the stop price = still optimistic).** Original (Jonathan, 14:50; /tmp/hold_to_target.py). 563 fills 9/1-9/30, bar sim (same-bar tie = stop; stop fills at stop or gap open; NO exit spread, so ~5 bp optimistic). +1% target: stop -0.5 -0.12%, -1 -0.12%, -1.5 -0.14%, -2 -0.17%, -3 -0.19%, -5 -0.18% -> WIDER STOPS ARE WORSE. Best shape +2/-0.5 -0.06% (23% win). All shapes beat the desk's -0.24% by 0.05-0.18% before exit spread, but every one is negative in H2 (9/16+). Next: re-score the top 3 shapes (+2/-0.5, +1/-1, +2/-1) with the exit at the bid and the desk's real exit fills, before any live change.

- [x] **Big size, 5-second hold (Jonathan, 15:00; /tmp/hold5s.py, real SIP quotes) - FAILS.** 178 fills 9/22-10/1, buy at the fill, sell at the SIP bid 5 s later: -12.0 bp/trade, win 7% (H1 -18.8 / H2 -6.6). The mid moves -1.6 bp in 5 s (median |move| 0.9 bp; unchanged on 47%) vs 10.4 bp to cross the spread both ways. Share count scales $ both ways, never the bp; bigger size also eats past the touch. Longer holds (/tmp/holdNs.py, 119 fills with 30 s of quotes): 5s -13.3, 10s -11.2, 15s -12.1, 20s -9.0, 25s -8.1 bp; win 7->27%; mid move never significant (|t|<=1). 30 s + tight stop on the SIP bid (/tmp/hold30stop.py): none -10.3, -0.05% -10.7 (83% stopped), -0.10% -11.0, -0.20% -10.5, -0.50% -10.2 bp - every stop ~-10 bp; tight stops fire on the spread itself (bought at the ask, bid already ~10 bp below).

- [x] **Overnight panel shows last night all day (Jonathan, 15:40 screenshot).** build_snapshot picks `book_night` = latest night with buy submits, so from the 09:31 sell until the 15:58 buy it shows SEP 30's rows (already sold; ERAS/MU/MXL say "buy ..." but they EXPIRED unfilled 9/30) while today's plan (plan_2026-10-01.json, 20 picks) sits unused. Fix after the 15:58 buy (no agent restart before it): once last night is sold and holding 0, show "TONIGHT - OCT 1" = today's 20 picks with open->now %, kept/dropped by the -1% filter, and the buy time; last night stays in the history row. Expired buys render "not filled". Same for overnight_live.

- [x] **Ratchet too fast? (Jonathan, 15:40) - NO.** leash_replay.py --set timing, 207 fills 9/23-9/30, live trail code on 1 s SIP prints, exit at the bid (live booked -19.6; replay live config -13.0 bp, hold 106 s). Slower is worse at every step: idle 15 s -14.2, 30 s -16.2, 60 s -18.6, decay off -20.0. Faster/bigger steps marginally better: idle 4 s -12.2, step 0.10R -11.9. Min hold: 30 s -14.0, 60 s -13.4, 90 s -14.2, 120 s -15.7, 180 s -15.0, 300 s -20.4, 600 s -29.5. Give 0.20R -11.6 (= 0.30R: give_max_pct/peak_give_pct cap binds). All gains ~1 bp, inside noise. No change.

- [x] **Decision data for #1 (spread gate) and #4 (spread unknown) - /tmp/spread_refusals.py, squares at the arm gate 9/29-10/1, one sample per name/reason per 5 min, real SIP quote at the moment, buy at ask -> sell at bid.** Armed squares (n 61): spread 0.08%, net 15m -0.15%. Refused spread_wide (n 49): real spread median 0.31% (mean 0.46%), net 15m -0.93%, and the MID fell -0.55% (worse movers, not just costlier). By band: 0.2-0.3% -0.22% (n 10), 0.3-0.4% +0.28% (n 9), 0.4-0.6% -2.58% (n 10), >1% -3.31% (n 4); 12 refused with a <0.2% SIP spread at that second still lost -0.87%. Refused spread_unknown (n 43): real spread median 0.31% (70% over 0.2%), net 15m -0.81% (09:30-09:46 -0.73%, 11:00+ -2.32%). Read: the spread gate is avoiding losers; unknown-spread names are mostly wide names. Small n (3 days).

- [x] **Overnight paper uses the whole account (Jonathan, 10/1 eve).** overnight_book.size_paper: 98% of min(equity, cash) at the buy, split equally across the names that pass the filter, whole shares, leftover handed out a share at a time. Tonight's 16 names would have been $24,497 (98%) vs $16,254 (65%) at $1,000/name. OVERNIGHT_EQUITY_FRAC overrides. Live test caps unchanged: Jonathan 10/1 eve - keep live as a small test ($100 book, $25/order, 1 share) for its first week (10/5-10/9); revisit full-equity live after that week.

## Open engineering items

- [x] Item 3: overnight paper P&L first-in-first-out (d8b1c8b, live).
- [x] Item 4: engine %R in minutes (d2630f1, live pre-open).
- [ ] Item 5: the 7 tests that were already failing. Running in a separate session (task_6be76155).
- [x] Item 6: replay tests engine changes. `--engine recompute` (26ebb1d) for indicator math; `--engine rt` (e575c9f) rebuilds the engine's bar store from recorded prints. Verified on the 9/30 FLY window: pre-fix code reproduces the fake square and buys FLY (~12:17:40, live 12:18:10); fixed code never squares.

## Context

- 9/30 replay: 55 opens, -1.4 bp/trade gross; costs ~10 bp a trade on top (spread).
- Every entry/exit/filter variant tested since August scores ~0 gross (see memory: exits-cannot-move-the-mean, entry-setups-are-noise-equivalent, the-universe-fades-at-every-horizon). Ideas worth trying are ones that change *which names* or *what information* the desk uses, not another threshold.
