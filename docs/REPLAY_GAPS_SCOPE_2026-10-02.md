# Exact replay: scope of the two remaining gaps (2026-10-02)

Basis: the full-day 10/1 exact replay run on the MacBook after 0dc6853 (fixes 1–2; the gate warmer fix 3 tested only on 09:30–09:50):
decisions 8,363 / 15,796 agree (52.9%), buys 11 / 73, read misses ~40,000. Pass bar: 0 misses, ≥ 99% decisions, ≥ 95% buys.
Tonight's mini run (16:01, `ai_reports/nightly/2026-10-01/exact_rerun.json`) will be the first with all three fixes and real keys; re-check the numbers below against it.

## Where the 7,433 mismatched decisions come from

| Share | What | Count |
|---|---|---|
| **73%** | **Seating**: the name was checked on one side only (live-only 2,702 + replay-only 2,760) | 5,462 |
| 27% | Same name, different decision | 1,971 |
| …of which | price freshness (`stale_quote` on exactly one side; both directions about equal) | ≥ 1,130 |
| | state carried from different earlier trades (`reentry_cooldown`) | ≥ 222 |
| | gate inputs (`spread_unknown`, `gap_unknown`, `pctr_not_live_alpaca`) | ≥ 130 |

(The output lists only the top 15 divergence kinds: 1,482 of the 1,971.)

**Seating is the bigger gap, and it feeds the other.** The two gaps aren't independent: names seated on one side only have no recorded reads, which produce most of the misses, which blank gate inputs, which change rankings and seating again.

## Gap A — seating (73% of mismatches): fix first

### Cause 1 (high confidence): the replay can't serve reads that live made once and then cached

- Read misses: ~40,000, of which **33,146 are Alpaca bar reads from `ai_entry_watch._gap_inputs_sip`** (the open-gap gate). Then quotes/latest 2,584 (`_latest_ask`), quotes 1,794 (`sip_spread_pct`), trades/latest 1,109 and snapshots 569.
- Live recorded only 8,699 bar reads for 264 (symbol, timeframe) keys, mostly 1–5 per key per day. **Daily bars were first fetched at 09:46 or later (median 09:56)**, because the gap value is cached for the day once the SIP 09:30 bar is servable.
- The replay serves a read made outside a decision pass by timestamp only: the latest recorded read at or before the replay clock, **if under 10 minutes old** (`Recording.alpaca_at`, `max_age=600`). 80% of bar reads were made outside a pass (6,950 / 8,699) by the background gate warmer. So the replay misses on every ask before live's first fetch, and again on every ask more than 10 minutes after it. The warmer retries every 30 s per name, all day.
- A missed gap, spread or volume-pace read leaves that input empty. The book server's `seat_priority` uses volume pace and %R, and admission requires arm-ready gates (`ai_watch_admit_require_arm_ready`). So misses change which names get seated.

**Fix:** serve **immutable** reads with no age limit. A request whose time window ended before it was recorded (daily bars, the 09:30 minute bar) returns the same bytes whenever it's asked, so the replay can serve the earliest recorded response at or after the window's end. For reads that change over time (latest quote or trade), serve the nearest recorded read and record its age instead of refusing. Also serve a read the replay asks *before* live first did, if live's first read came within a few seconds (warmer timing jitter).
- Files: `desk_io.py` (`Recording.alpaca_at`, `alpaca_key`: carry the original time params on the record). Replay only; **no change to the live desk.**
- Effort: about 3–4 hours with tests.
- Expected: most of the 33k bar misses and a share of the quote misses disappear. Seating should converge noticeably, but how much needs one re-run to measure.

### Cause 2 (medium confidence): process-local timers start at a different moment

- Live booted at 05:41; the replay starts cold at 09:30. Process-local cadences (the soft seed's 300 s interval, `_SOFT_SEED_LAST_TS`; stream-strike grace; scout TTLs) and in-memory caches (spread, gap, volume pace, square streaks) therefore run on a different phase and start empty, so admissions happen at different moments.

**Fix:** pre-roll the replay from the desk's boot time (or 09:00) with decisions not scored, so timers and caches are in phase at 09:30.
- Files: `tools/replay_session.py` (`--exact`: a pre-roll window, score from 09:30).
- Effort: about 2 hours. Cost: a longer replay, but premarket is quiet (the desk doesn't trade premarket).

### Cause 3 (to verify first, 30 min): does the warmer read the replay clock or the real one?

The gate warmer's `_cached._read` and `_warm` loop call `time.time()`. If the replay doesn't patch that clock for the warmer, then the "same day" checks compare 10/1 cache stamps against today's date. Every cached value would read as expired and be re-fetched forever, which would fit the size of the 33k. Check: run the 09:30–09:50 slice with a print of `time.time()` inside `_read`. If confirmed, the fix is a one-line clock injection.

## Gap B — price freshness (≥ 1,130 same-name mismatches)

- **It isn't the stream missing from the recording.** The Finnhub websocket runs in `signal_engine.py`. The desk process reads prices only through the dashboard's `/api/state`, which *is* recorded (40,585 dash records on 10/1), with every `*_age_sec` stored as an epoch.
- **The evidence points to timing.** Mismatches go both ways in near-equal numbers (live stale/replay fresh 272 + 218 + 60; replay stale/live fresh 233 + 191 + 45). That's what a sub-second to few-second clock offset around the 15 s staleness limit looks like, not a missing data source.
- Likely mechanisms:
  1. Live fetches `/api/state` up to 4×/s on its own schedule, while the replay serves the doc at its pass clock. The desk's process-local price clocks (`_LAST_QUOTE_TS`, `align_stream_clock_if_field_young`, the honesty restamp) advance per fetch, so fewer or differently timed fetches give different ages.
  2. Ages are rebuilt at the replay's `t` rather than at the moment live received the doc.

**Fix:** record, per decision pass, the dash fetch the pass actually used (fetch time plus its sequence number). The replay then serves exactly that fetch, aged as of its receipt, and replays the fetch thread's cadence (each recorded fetch in order) so the process-local price clocks advance as they did live.
- Files: `desk_io.py` (`record_dash`/`dash_at`: fetch sequence), and wherever ai_trading fetches `/api/state` (tag the fetch). **One tiny live change:** a counter per fetch, negligible cost.
- Effort: about 4–6 hours, plus one live day to record with the new tag. Pre-change days can't be fixed retroactively.
- Expected: most of the ≥ 1,130 freshness mismatches. The residue would be true thread races, which no replay reproduces exactly.

Recording the Finnhub stream itself (the original idea) isn't needed for the desk process and would add ~10⁵–10⁶ prints a day to the recording for no gain.

## Recommended order

1. **Cause 3 check** (30 min). It could explain most of the misses by itself.
2. **Gap A cause 1:** immutable and nearest reads (3–4 h). Re-run 10/1 and 10/2.
3. **Gap A cause 2:** pre-roll from boot (2 h). Re-run.
4. **Gap B:** dash fetch sequencing (4–6 h + one recorded day).

Total about 10–13 hours of work, plus re-runs after the close.

## Is the pass bar realistic?

- **0 misses: no, as written.** Any read the replay makes that live didn't (a different seat, a retry) is a miss by definition. Better: 0 misses **for reads live made**, and report the rest as "replay-only reads".
- **≥ 99% decisions: probably not.** Thread timing in a multi-threaded desk (warmer, dash fetcher, polls) can't be reproduced to the millisecond. A realistic target after the steps above is **≥ 90–95%** on names seated on both sides, with seating overlap reported separately.
- **≥ 95% buys within 5 s: plausible** once seating converges, because buys are the most constrained decisions. Measure after step 3 before setting it.

Suggest splitting the verdict into three numbers (seat overlap, same-seat decision agreement, buy recall) instead of one pass/fail, so a regression in any one is visible.
