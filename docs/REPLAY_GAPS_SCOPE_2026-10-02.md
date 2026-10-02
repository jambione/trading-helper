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

### Cause 1 — measured and FALSIFIED: the 10-minute serving window

Hypothesis: the replay misses reads that live made once and cached, because it serves timestamp-matched reads only up to 600 s old.
Test (2026-10-02, 10/1 10:00–10:20, all three fixes, `REPLAY_MAX_AGE` 600 vs 86,400):

| read age limit | misses | decisions agree | live-only names | replay-only names |
|---|---|---|---|---|
| 600 s (nightly default) | 771 | 61.5% | 85 | 104 |
| 86,400 s | 683 | 61.3% | 90 | 96 |

No real effect. The remaining misses are reads live **never made**: a symbol or parameters it never asked for. Also, the 33,146 `_gap_inputs_sip` misses quoted above came from the run *before* the gate-warmer fix (fix 3). With it, this window has ~770 misses in 20 minutes, spread across `vol_now_iex` 243, `_gap_inputs_sip` 143, `_latest_ask` 132, `prime_quotes` 89, `day_high_iex` 70 and `_rvol_pace_inputs` 46.

**Next diagnostic (about 1 hour):** log the symbol and parameters on each miss, then split the misses into (a) names seated on the replay side only, which are a *consequence* of seating, and (b) names seated on both sides, which would be a *cause*. Separately, log each name's first divergence from seating (admit vs drop and the reason) to find the first decision that splits. Seating's root cause stays open until then.

### Cause 2 — already handled: the replay pre-rolls from the desk's boot

Checked 2026-10-02: `run_exact` already replays every recorded pass from the desk's last boot (05:41 on 10/1; `[exact] boot ... 10654 recorded passes to 09:50`) and scores only from `--start`. Process-local timers and caches are in phase by 09:30. No work needed.

### Cause 3 — ruled out: the replay clock reaches the gate warmer

Checked 2026-10-02: `patch_clocks` swaps the `time` module in every desk module, so `time.time()` inside the warmer's `_cached._read` and `_warm` reads the replay clock. `_gap_inputs_sip` takes `t` explicitly and doesn't fetch before 09:46. No desk-path call reads the real clock through `strftime`/`localtime` without a timestamp. The only real-clock reads are harmless: `book_server.py:456` (shadow-log folder name) and `ai_entry_watch.py:16600` (entries-today count for a per-name cap that is off, `ai_watch_max_entries_per_symbol_day=0`). The second is still a latent replay bug if that cap is ever turned on.

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

1. ~~Cause 3 check~~: done, ruled out. ~~Cause 2 pre-roll~~: already in place.
2. ~~Gap A cause 1 (serving window)~~: falsified by measurement. **Next: miss and seating attribution** (about 1 h), then fix whatever it points to.
3. **Gap B:** dash fetch sequencing (4–6 h + one recorded day).

Total about 8–10 hours of work, plus re-runs after the close.

## Is the pass bar realistic?

- **0 misses: no, as written.** Any read the replay makes that live didn't (a different seat, a retry) is a miss by definition. Better: 0 misses **for reads live made**, and report the rest as "replay-only reads".
- **≥ 99% decisions: probably not.** Thread timing in a multi-threaded desk (warmer, dash fetcher, polls) can't be reproduced to the millisecond. A realistic target after the steps above is **≥ 90–95%** on names seated on both sides, with seating overlap reported separately.
- **≥ 95% buys within 5 s: plausible** once seating converges, because buys are the most constrained decisions. Measure after step 3 before setting it.

Suggest splitting the verdict into three numbers (seat overlap, same-seat decision agreement, buy recall) instead of one pass/fail, so a regression in any one is visible.
