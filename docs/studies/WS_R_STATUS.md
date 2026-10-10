# Workstream R status (synthetic-day replay), 2026-10-10 — WIP, stopped for usage

Branch `ws-r-synth-day` (pushed from the MacBook). No held-out day has been run or touched.

## Built
- `tools/replay_session.py --universe FILE` (alias `--synthetic-universe`): runs the live desk code on a day without a recording.
  - Watchlist: universe rows go through the desk's own sync (`ew.desk_candidate_rows` is patched to `SynthDay.candidates`). Inclusion gate, grace, slots and book server are the live code. A name is offered from `first_ts` for as long as `still_offered()` holds: `intervals`/`last_ts` when the row has them. Without them, each source's seed rule runs on current numbers: movers/tight need the day-change and price floors, momentum needs pct > `ai_watch_min_pct_change`, and the other sources stay offered all day.
  - Price and clock: the last SIP last-sale print at or before the clock. The age is clock minus print time, so price and age come from one print. Prints are fetched once per day (REST, 2 threads, 429 backoff), thinned to the first and last print per second, and cached in `$REPLAY_CACHE_DIR/trades_v1_DAY.pkl`.
  - Engine rows: `SynthEngine.state` on IEX premarket + IEX session 1-minute bars (completed minutes), memoized per minute. This is pinned to recompute/iex. `rt_price`/age come from the same SIP print.
  - Fills: an entry fills at the first SIP print strictly after the decision. With no print within `--entry-print-window` (60 s) there is no fill, and it is counted. An exit fills at the first print after the exit decision (`FakeBroker.fill_fn`). `entry_ts`/`exit_ts` are the fill times; `decision_ts`/`exit_decision_ts` are kept.
  - Gate inputs: historical SIP only (`RecordedInputs` is emptied). Equity comes from `--equity`, else `<repo>/ai_positions_state.json`.
  - Config: `--config-file F` (default `<repo>/config/bot_config.json`, read-only) or `--config-stream F.jsonl` (`{ts, config}`, used for fidelity). The source and sha256 are logged in the result.
  - Output: same shape as the recorded replay, plus `mode: synthetic_day`, `synthetic{...}` (universe/config sha, equity, trade-fetch stats) and `vacuity{...}`. Vacuity holds arm polls/checks, wr_* refusals, place calls, entry_no_print, exit_no_print, ob feeds, names served/offered, bars missing and trades failed. Recorded runs get `vacuity` too.
  - Guards: a synthetic run before 2026-09-24 is REFUSED unless `--allow-held-out` is passed. `--universe` cannot be combined with `--fidelity/--exact/--live-book/--warm-book`. Path args are made absolute before the re-exec.
  - Recorded-day behaviour is unchanged. The existing replay tests pass (19); `tests/test_replay_synth_day.py` has 17 passing. The FULL suite has NOT been run yet.
- `tools/studies/synth_day_fidelity.py export|run|score`: implements part A per the amended prereg (eb0c863). Variants are base / st / wrrsi_st. Each day runs with its own config stream and its 09:30 dashboard equity. `score` prints trades/day, overlap both ways within 90 s, gross/net bp, per-day and pooled diffs, the paired (wrrsi_st − base) difference, per-day sd and the gate verdict, and writes `OUT/fidelity_<kind>.json` with the vacuity log. Costing uses a private copy of the cache (`/tmp/ws_r_out/costing_cache.json`).

## Mini state (`ssh mac-mini-away`)
- Code copy: `/tmp/ws_r`, a clone at 01f6e9c, detached. `.venv`, `ai_reports` and `config/secrets.json` are symlinks to the main repo (read-only use). Cache: `/tmp/ws_r_cache` (copies of the recorded-day bars and sip_spread).
- Exports are done: `/tmp/ws_r_out/DAY-{first,iv,config.jsonl,equity.json}` for all 12 days.
  - KNOWN BUG: `2026-09-24-equity.json` is null because that recording has no api/state. Before running syn on 9/24, set it from `ai_positions_state.json` (about 2.2k).
- RUNNING (nohup, nice 10, safe to leave): `/tmp/ws_r_out/run_rec.sh` (PID 49926). This is leg (a), the recorded replay at `--sha eb0c863`, base → st → wrrsi_st × 12 days, about 4-9 min each. Status is in `/tmp/ws_r_out/rec/status.txt`, results in `/tmp/ws_r_out/rec/DAY-VAR.json`.
- RUNNING (nohup): the smoke synthetic 10-08 base (PIDs 50555/50560). It fetched prints for 146 names in 398 s (1268 requests, 3 names failed). Log: `/tmp/ws_r_out/smoke/2026-10-08-base.log`. It has not produced wall time or trade numbers yet. This run used the pre-fix `thin_prints`, which duplicates single-print seconds. That is harmless, but the cache has duplicates; delete `/tmp/ws_r_cache/trades_v1_2026-10-08.pkl` to rebuild it clean.

## Next (in order)
1. Read the smoke log/JSON. Check trades>0, arm_checks>0, entry_no_print, wall time (target ≤ 10 min per day-variant once prints are cached; the first fetch is about 6.5 min per day). Fix errors.
2. Update `/tmp/ws_r` to this branch: `cd /tmp/ws_r && git fetch -q gh ws-r-synth-day && git checkout -q --detach FETCH_HEAD`.
3. Leg (b), with at most 2 replays at once in total (rec still occupies 1 lane):
   `cd /tmp/ws_r && REPLAY_CACHE_DIR=/tmp/ws_r_cache nohup .venv/bin/python tools/studies/synth_day_fidelity.py run --out /tmp/ws_r_out --leg syn --universe-kind first --lane 0/1 2026-09-24 2026-09-25 2026-09-28 2026-09-29 2026-09-30 2026-10-01 2026-10-02 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09 > /tmp/ws_r_out/syn.log 2>&1 &`
   (Optionally run again with `--universe-kind iv` as a diagnostic; there are no intervals for 9/24.)
4. Score: `.venv/bin/python tools/studies/synth_day_fidelity.py score --out /tmp/ws_r_out --universe-kind first <12 days>`.
5. Run the full test suite, then the final commit.

## Open issues / not modelled
- Finnhub stream gaps and drops are not modelled; stale-tape refusals come only from SIP print gaps.
- Momentum-panel flags and dashboard extras, research/trending/bro boards, the soft-seed/book-server inputs from panels (panels are written empty), and the forming-bar %R are not modelled.
- Retention without intervals is a model of the panel; part B and `--universe-kind iv` show its effect.
- Config is followed from the stream, but dashboard equity is fixed at the 09:30 value.
- About 2% of names fail the print fetch (3/146 on 10-08); they are listed in vacuity.
