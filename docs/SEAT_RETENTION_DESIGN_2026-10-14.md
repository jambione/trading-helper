# Seat retention: keep the book full (design, 2026-10-14)

`docs/PLAN_2026-10-14.md` stage 2 / queue item 3. Operator goal: a full, fresh book from 09:45 with several opens at once.

## The evidence (corrected the same morning)
`tools/stale_stay_dig.py` labels 85-95% of recent drops `stream_then_silent`, but its classifier keys on elapsed time, not the drop reason. The real reasons, from `ai_reports/events.jsonl`:

| day | drops | `dead_unknown` | `never_armable` | other |
|---|---:|---:|---:|---:|
| 2026-10-13 | 342 | 272 | 59 | 11 |
| 2026-10-12 | 2,069 | 1,370 | 625 | 74 |
| 2026-10-09 | 1,523 | 1,051 | 422 | 50 |

`dead_unknown` (`ai_entry_watch._maybe_dead_unknown_evict`) evicts a watching seat after **30 s** (`ai_watch_dead_seat_evict_sec`) when its block is in `_DEAD_UNKNOWN_BLOCKS` (stale quote/tape, no quote, no RSI, no %R, **exh_falling**) or its %R never painted. The blocks behind the 10/12 drops: `exh_falling` 747, `stale_quote` 320, `pctr_not_live_alpaca` 206. On 10/13: `stale_quote` 91, `pctr_not_live_alpaca` 81, `spread_unknown` 34.

**It is thrash, not loss.** The same names are evicted and re-seated all day: INDA 47 times on 10/13; EQNR and TTE 77 times each on 10/12; 42-61 unique names behind 272-1,370 drops.

## Why it matters for entries
A seat whose %R dips is evicted within 30 s. It comes back only after it is rising again, then needs its stream, %R and square confirmation rebuilt before it can arm. **The eviction rule itself makes entries late** - the operator's observation that the desk opens about five minutes late. Each churn also spends one of the ~50 real-time subscriptions and admission work.

## Design options
1. **Do not evict on `exh_falling`.** A falling %R is a reason not to buy now, not a reason to leave the board; the arm already refuses on it. Keep the seat warm so it can arm the moment the line turns.
2. **Separate data-readiness from eviction.** `stale_quote` / `pctr_not_live_*` are feed and warm-up states; give them a longer clock (e.g. 180 s) and evict only when a better candidate needs the stream slot (rotate stream slots by value: lowest-value seat goes first).
3. **Judge stale quotes per name** against that name's own recent IEX gap, not a fixed clock (memory: iex-trade-gaps-are-not-staleness).
4. **Anti-thrash.** A name evicted N times in M minutes keeps its seat (or is barred for the day if it never arms), instead of cycling.

## How to test
- **Replay:** `replay_session.py --engine rt` rebuilds the stream bar store from recorded prints (it reproduced the FLY stale-carry bug), so options 1–4 can be replayed against the same sessions once coded. Metrics: average seats and seats at 13:00, opens per 10 minutes (bar: ≥ 1), concurrency, gross per trade (must not fall).
- **Prereg** before code: option 1 first (smallest change, biggest expected effect), with the live config pinned as in `no_green_scratch_prereg.json`, ten recorded sessions, after-hours runs at ≤ 3 at a time.

## Order
1. Prereg + replay option 1 first: it is a one-line membership change (`exh_falling` out of `_DEAD_UNKNOWN_BLOCKS`, behind a config switch, default unchanged). Metrics: drops and re-seats per day, average seats, opens per 10 min, minutes from a name's %R turning up to its arm, gross per trade.
2. Then option 2 (longer clock for feed-state blocks), then 4.
3. Fix `stale_stay_dig.py` to bucket by drop reason, so the next dig does not mislabel thrash as silence.
