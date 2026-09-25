# After-close log — Fri 2026-09-25

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

## 3. Trader startup still ~3 min (OPEN)

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
