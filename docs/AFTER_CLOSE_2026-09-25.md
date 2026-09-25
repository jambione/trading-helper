# After-close log — Fri 2026-09-25

**Standard:** pass bar = full fresh book (>=10 seated by 09:40, >=6 armable,
<10% data-blocked) and >=1 open per 10 min; goal = ~5 opens per 10 min, 2-3
open at once, 3-8 min holds, stacking to max 5, all day. Judge every fix
against both.

**FIRST TONIGHT — cross funnel on today's recording.** For every -50 cross
on a seed name (RTH): was it seated with fresh data, refused at the door
(which gate), dropped from the book earlier, or not yet listed (and how long
from first listed to seated)? This splits "wider" vs "keep seated" vs
"faster". Then replay today with the top fix and count added opens vs the
1-per-10 bar. Context (12:19): the book was healthy (14 seated, 8 armable,
79% fresh) but the whole book sat at fast %R -7..-26 in a broad up-leg — no
dips, no crosses. More seated names = more chances that something is dipping.

Running list of things seen during the session, to work through after the
close. Newest at the bottom. Replay/recording for today lands in
`~/session_snapshots/2026-09-25/` on the mini at 16:05.

## 1. Slow start — 0 opens by 09:47

- 09:47 snapshot: 11 seated (7 momentum, 4 movers), only 3 armable, 4 data-blocked
  (36%), 7/11 seated names with a price <= 15 s old. 0 opens.
- Arm refusals 09:30-09:47: `tape_only` 210, `spread_wide` 171, `wait_mid_rise` 170,
  `spread_unknown` 43, `pctr_not_live_alpaca` 23.
- Engine tracked 39 names against the 30-slot setting: the cap only limits the
  desk's pushes; Discord watchlist names (own slots since cebf3a3) and cheap
  momentum names add on top, so the feed is over capacity again.
- Momentum names could not pass the 0.20% SIP spread gate on tick size ($1-5):
  556 momentum `spread_wide` refusals by 09:47. Exempted at 09:56 (c1b54b0).
- Same pattern as 9/24 (first fill 11:05). To answer tonight: which names crossed
  -50 between 09:30 and 10:30, were they seated with fresh data, and what refused them.

## Changes made during the session

| Time | Change | Commit |
|---|---|---|
| 09:56 | Momentum exempt from SIP spread gate (restart ~45 s) | c1b54b0 |
| 10:39 | Dashboard: no self-HTTP, cache-only gates (fixes post-restart freeze) | 55e7ced |
| 11:28 | Trader + dashboard gate inputs non-blocking (`ai_watch_async_gates`); movers keeps last list on a failed bars call. Restart with FFBC open (user OK); trader up in 27 s (was ~3 min) | bb58ab4, 73b6622 |
| 11:41 | Held-quote REST refresh off the book thread (background, single-flight, 3 s floor). Desk flat at restart; trader up in 18 s; book 41 writes/90 s, max gap 7.0 s (was 20.5 s) | b96999c |
| 11:51 | Removed the Mobile Trader L2 bridge from the dashboard (~50 Alpaca req/min in RTH). Desk flat at restart | b739543 |

## 2. Post-restart freeze: dashboard self-deadlock (FIXED 10:39, 55e7ced)

- Every restart froze the desk 5-8 min: first /api/state snapshot built in 320 s
  ("[SNAP] rebuilt in 320.25s"), the trader's startup sync waited on it, no
  opens and no exit management meanwhile. Hit at 09:56, 10:09, 10:24.
- faulthandler dump (b5e12cf) showed the cause: overlay_ai_book_live_prices ->
  ai_entry_watch.apply_tape_blocker per book row inside _snapshot; live_print ->
  dashboard_state() HTTP-called the dashboard's OWN /api/state (4 s timeout per
  row; hot since Grok's d5d439b), and should_arm_buy fetched SIP bars/quotes
  per row on cold caches.
- Fix: in the dashboard process only, dashboard_state reads the cached
  snapshot in-process and gate inputs are cache-only with a background warmer.
  After fix: first snapshot 0.04 s.
- KORU (09:56) sold at the broker in the same second as a restart; the desk
  showed it OPEN for 8 min until the trader reconciled. Broker was flat.

## 3. Trader startup still ~3 min (FIXED 11:28, bb58ab4: 27 s)

- After 55e7ced the trader still took 3 min (10:39:35 -> 10:42:38) to start its
  book thread: `_publish_book()` runs on the main thread before the book thread
  and fetches SIP spread/gap/pace per candidate on cold caches. Exits are not
  managed until it finishes. Fix after close: start the book thread first
  and/or keep the startup sync cache-only. Rule until then: no mid-session
  restarts.

## 4. Morning opens and blockers (09:30-10:42)

- 3 opens: KORU 09:52 (+0.7%), TOST 10:23 (-0.2%), FLY 10:23 (-2.1%).
- ~30 min of the first 75 had the trader down or frozen (restarts 09:56, 10:04,
  10:09, 10:24, 10:39) — most of the missing opens.
- Arm reasons 09:30-10:42: wait_mid_rise 587, tape_only 549 (stale price),
  spread_wide 199, pctr_not_live_alpaca 101, extended_cheap 95 (cheap momentum
  names judged extended), spread_unknown 77.

## 5. Momentum cheap-name gates (after close, no restart today)

- Even with the $1 floor (7f5bcac) and the spread exemption (c1b54b0), $1-5
  momentum names get refused by two cheap-name gates: `extended_cheap`
  (ai_entry_watch.py ~15773, 76+ refusals by 10:49) and `cheap_ob_band`
  (~15765). Momentum test: 0 opens by 10:49.
- To do: decide whether the momentum test exempts them; replay today's
  recording with and without, scored after real spread.

## 6. Book shrank mid-morning

- 10:46: 13 seated / 6 armable. 10:49: 7 seated / 2 armable, 29% data-blocked,
  only 4/7 seated names fresh. Engine 34 names vs 30 target; the 30 limits only
  the desk's pushes (Discord/momentum watchlist adds on top). Day roster 0 seats.
- To do: make the whole data feed respect its capacity; see why seated names go
  stale and why the roster never seats.

## 7. Attack plan: stale prices (main blocker to all-day opens)

A seated name is "stale" when its last trade print is > 15 s old. Three causes,
three different fixes, so measure first.

**Measure (today's recording):** for every `tape_only` / stale refusal, classify
the name at that moment using the prints stream (every price, source, true
age) against SIP historical trades (ground truth for how often it traded):
1. Coverage — not subscribed / no Finnhub prints at all.
2. Thin trading — subscribed, but real trades > 15 s apart (price not moving,
   the rule reads "no recent trade" as "no price"). Expect cheap momentum
   names here.
3. Lag — the trade happened but reached the desk late (engine -> dashboard ->
   trader).
Output: the split (e.g. 60/30/10) and which fix pays.

**Fixes by cause:**
| Cause | Fix |
|---|---|
| 1 Coverage | Whole data feed respects capacity, not just desk pushes: seated + held first, then armable by rank, then the Discord watchlist; no churn. Extend slot priority (4cf87ec) to the Finnhub subscription list. |
| 2 Thin names | Seat names that trade constantly: add trades-per-minute to seat eligibility and rank. Consider the IEX quote clock as proof of a current price when the last trade is older. |
| 3 Lag | Trader reads engine prints directly instead of the dashboard's merged view. |

**Then:** replay today with the chosen fix and report how many more opens it
would have produced, before anything goes live.

Initial read (to confirm): mostly cause 1 (freshness fell as the watchlist grew;
engine at 34-44 names vs 30), then cause 2.

Update 10:59: seated freshness fell to 2/10 (70% data-blocked). Not an outage:
Finnhub connected (38 subs), engine on realtime for 29/33 names, but only 14/33
names printed a trade in the last 15 s (median age 16.8 s). Late-morning lull
points at cause 2 (thin trading vs the 15 s rule) more than cause 1. The
measurement should split by time of day.

## 8. Book lag: the trader's book thread stalls on Alpaca lookups

- 11:12 measurement: book writes 6, 2, 3, 12, 4, 16, 4 s apart (should be
  every 2-3 s); earlier a 21 s gap. Dashboard snapshots fine (0.01-0.04 s).
- Cause: the book sync runs the admission gates, which fetch SIP spread / open
  gap / volume pace from Alpaca per candidate, blocking, on the book thread
  (25 lookups in that minute). While they run, the book and its price stamps
  stop updating.
- It feeds staleness (item 7): a 16 s stall ages every seated price past the
  15 s limit. Split the staleness measurement into "our own stalls" vs market.
- Same root as the dashboard freeze (fixed 55e7ced) and the trader's 3-min
  startup (item 3). Fix: book thread reads gate inputs cache-only; a
  background thread keeps the caches warm. Needs a restart -> after close.
- 11:28 shipped (bb58ab4). Book gaps after it: still 10.3, 10.5, 20.5 s. Different
  cause, see item 10.

## 9. Movers list wiped by a rate limit (FIXED 11:28, 73b6622)

- 11:24 movers_stocks.json went to 0 rows: the daily-bars call got 429 "too many
  requests", every candidate then failed measurement and the empty list was
  written. Book fell to 6 seated / 1 armable. Now a failed bars call keeps
  the last list (valid 15 min). Likely also the 10:49 shrink (item 6).

## 10. The shared Alpaca budget is saturated; 429 retries freeze the book

- faulthandler samples 11:30: 6/10 on the trader's book thread were inside
  alpaca rest.py `time.sleep(retry_wait)`: refresh_open_position_quotes ->
  get_stock_latest_quote, run every book tick for held names. alpaca-py
  retries 429 3x with 3 s sleeps, so one call blocks up to 12 s.
- 429s seen today in engine (19), dashboard (5), trader (2), movers (7).
- To do: count requests/min by process (L2 panel ~106/min is the prime
  suspect, see memory note); the dashboard and trader warmers both fetch the
  same gate inputs (dedupe: one process fetches, the other reads); held-quote
  refresh should not run every 2-3 s tick on the book thread; alpaca clients
  on hot paths should not sleep-retry.
- 11:41 quick fix shipped (b96999c): held-quote refresh runs in a background
  thread. First read taken while flat, so the held path was not exercised;
  re-measure with positions open. Remaining 7 s gaps: find the cause.

## 11. Book server (shadow) findings, 11:46

- Ran all day (910 rows 04:00-11:46, survived restarts). Would seat 12 every RTH
  hour; live seated 7-10; only ~5 in common. Supply for a full book exists.
- ~40% of its ranking stands on missing inputs (pctr/pace known for ~12 of ~21).
- It ranks names the live gates refuse (GME 6th while -5.7% on the day). Before
  it seats live: skip gate-refused names; replay whether its extra names
  crossed -50 today (would a fuller book have opened more?).

Update 13:06:
- Every one of the 11 opens so far was in its would-seat set within the 30 min
  before the open; 7/11 at that exact moment. Its ranking contains the names
  that trade.
- Inputs improved after the async-gate + L2 fixes: 13:00 hour pctr 18/24, pace
  23/24 (09:00 hour: 10/20, 9/20). Would-vs-live overlap 4.2 -> 6.9 of 12.
- 13:06 it would add EMBJ/EWJ/DB (below -50, setting up) and drop RKLB (-79,
  also set up): priority weighs runway + pace, not "about to cross".

Before it seats live:
1. Skip names the live gates refuse (CVS float_too_big, red names like HIMS
   -0.9%, gap-down, band).
2. Give weight to setup state (fast %R below -50 with slow rising) so it does
   not drop names about to cross.
3. Replay today with it seating: do its extra seats produce extra -50 crosses
   and opens? That is the go/no-go number against the 1-per-10 bar.

## 12. The door refuses most of the supply (11:50)

- 70 distinct names refused admission in 10 min (5 seated): thin_rvol 35,
  below_min_price 7, above_max_price 6, not_uptrend 5, spread_wide 4, pct_low 4,
  stale_tape_admit 3, float_too_big 2 (CVS, PYPL), gapped_down 1, red 1.
- By source: movers 32, trending 24, agy 5, xai 4, momentum 4. The seeds are
  curated; the door second-guesses them. thin_rvol is the big one: measure
  tonight whether refused thin_rvol names crossed -50 and how they did.

## 13. Entry review at 12:56 (11 round trips; tool: scratchpad entry_review.py)

- 8/11 winners, mean 0.00%. Holds 3-11 min (fits the 3-8 goal) except FFBC
  11:06 dead_trade at 30 min. FLY -2.07% erased the other winners.
- Quality: liquid names up 1-6% on the day, mostly Movers; momentum only 2
  (TOST, KOPN) — the momentum test still has no sample.
- Timing: 7/10 up at +5 min; by +30 min 5/9 below entry (KORU -2.6%,
  FLY -2.1%). The entry catches a short pop and the universe fade takes over,
  so short holds fit. Median MFE ~+0.4% vs 0.05-0.2% spread: thin margin.
- 6/11 entries in the top 10% of the prior 30-min range (KORU 90, FLY 97,
  FFBC 92/94, KOPN 100, CDNA 93): the -50 cross can fire on a shallow dip
  inside a strong run.

To do:
1. Test the 30-min range position as an ARM gate (cap ~90) on today's
   recording; admission-side evidence already showed a ~90 cap cuts the fade
   (-1.81% -> -1.15%). Report opens lost vs loss avoided — it must not sink
   the opens rate below the pass bar.
2. FLY exit slippage: sold at -2.07% vs about -1.85% worst bar low (worked
   shelf $24.07, filled $23.81 during the 10:24 freeze). Check whether the
   freeze alone explains it (exits are later work).
3. Rerun entry_review.py on the full day after 16:05 for the final numbers.

## 14. Dashboard shows green "ready" rows labelled "stale quote" (13:15)

- Green row = server phase "ready" (ai_entry_watch.py ~4972: price in zone and
  not _poller_blocked). The label comes from feeds.js _bookTapeStale (last
  TRADE print age). They disagree on thin names: EWJ/IONQ quote age 0 s but
  last trade old; GFS 51 s, TEM 25 s, EMBJ 19 s.
- The arm follows the trade print (tape_only), so the label is the truth; the
  green is misleading. Fix: one clock for both (row colour must not say ready
  while the arm would refuse on tape age).
- Same evidence as item 7 cause 2 (thin trading): a live IEX quote with no
  trade in 15 s is refused as "no price". Test "fresh quote counts as live"
  in the replay.

## 15. Room below the day's high back into the book-server ranking

Source: docs/BOLLINGER_STUDY_2026-09-25.md (0d8f490), 3,073 in-band -50
crosses, train <= 9/17 / test 9/18-9/24.
- Bollinger %B / bandwidth: no evidence; do not add.
- Room below HOD (`dist_hod_pct`): names nearest their high (>= -1.2%) did
  worse than names >= 2.7% below it: ret60 T3-T1 -0.36% (t -3.8) test, -0.84%
  (t -3.7) by symbol-day, 0/7 days the other way, holds within day_chg x pace.
- Caveat: in the held-out linear rank it did NOT clearly improve the ranking
  (Spearman +0.019 vs +0.040 baseline forward; SD-weighted top-bottom -0.06%
  forward, +0.47% reverse). The tercile effect is solid; its value as a
  ranking weight is unproven.
- Same signal as item 13 (6/11 entries in the top 10% of the 30-min range)
  and the memory note on admission range position (~90 cap cuts the fade).
- It was dropped in the book-server rewrite only because nothing live
  computed it (book_server.py header).

To do:
1. Engine tracks each name's running RTH high from the 1m bars it already
   builds; expose it on the candidate row (remember the indicator whitelist:
   the poll's indicator dict must carry the new field).
2. Replay by symbol-day with room-below-HOD in the ranking (and, separately,
   as a seat/arm filter) vs without. Count opens as well as ret60: the ranking
   also drives live slot priority (ai_watch_slot_priority=true), so it must
   not cost opens below the 1-per-10 bar.
3. Only then weight it in seat_priority.
