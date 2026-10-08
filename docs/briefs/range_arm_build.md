# Brief: build the range-entry variant (replay first, switched off live)

Branch: create `range-arm` from `master-mac`; push only to it. Do not merge. The spec is
`docs/studies/range_arm_replay_prereg.json` and every rule in it is binding. Where it is silent, choose the simplest
reading, write it in a `RANGE_ARM_RESOLUTIONS` comment block next to the code, and list it in your hand-back.

## What to build

1. **Config** (`config.py`, defaults OFF): `ai_watch_range_arm` (False), `ai_exit_range` (False),
   `ai_watch_range_sup_band_pct` 0.30, `ai_watch_range_min_room_pct` 0.40, `ai_watch_range_pr_max` -20,
   `ai_watch_range_engine_max_age_sec` 90, `ai_exit_range_res_pad_pct` 0.02, `ai_exit_range_time_min` 30.
   Add them to the config fingerprint (`learn_stamps.py`; `tests/test_setup_audit.py` fails otherwise) and to the
   effective/safe key lists the way `ai_exit_triangle_engine_only` was added in 5479bd8.
2. **Entry** (`ai_entry_watch.py`): when `ai_watch_range_arm` is on, the square / presquare / last-mode arms do not
   fire; instead a pure function `range_arm_ok(ob_fields, engine_sig, prev_engine_key, cfg, now) -> (ok, why)` decides,
   per the prereg entry rule. Support distance is `ai_entry_watch._ob_support_pct` (added in 7352d72); room is
   `ob_room_pct`; stale order-block readings (`_ob_reading_stale`) mean no arm. %R comes from the ENGINE line on the
   record (dashboard `signal_proximity` / the indicator the poll built), never from `apply_live_exhaustion`'s
   recompute; "rising" means higher than on the previous DISTINCT engine read (key = engine newest-bar time from
   `bars_age_sec` plus the value, as in docs/design/RUN_MANAGER_2026-10-08.md). Every other admission and arm gate stays
   in force. Record `range_s_btm` and `range_r_btm` (from `ob_observe` charted blocks, without changing
   `ob_observe.py` or `tools/order_blocks.py`, which are pinned by other studies) on the decision and the position.
3. **Exit** (`ai_positions.py`): when `ai_exit_range` is on and the position carries `range_r_btm`, a pure function
   `range_exit_due(pos, price, cfg, now) -> (sell, reason)` implements target / stop / 30-min time; for those
   positions the ratchet, decay leash, no-progress, dead-trade, triangle and %R dump do not act; 15:50 flatten stays.
4. **Replay** (`tools/replay_session.py`): `FakeBroker._live_exit` calls the same `range_exit_due` for such positions,
   and the arm path uses the same `range_arm_ok`. Prove with a test that the replay produces order-block readings
   (non-empty `ob_room_pct` and support distance) on recorded sessions; if the replay has no order-block bar store,
   wire it the way `ob_narrow` reads it and say so.
5. **Nightly**: add to `scripts/tight_trail_replay.sh`:
   `R range_arm --set ai_watch_range_arm=true --set ai_exit_range=true`
6. **Per-input check** (prereg): log per session the shares and counts it lists, into the replay's per-variant output.

## Tests (required, no network)

Pure-function tests for every entry and exit rule, including near-misses (0.31% above support, 0.39% room, %R -19,
%R not rising, same engine key twice, stale engine read, stale order-block reading, inside resistance); the exit race
(target vs stop on the same poll: stop wins); the knobs-off path is byte-identical to today (existing tests stay green);
the replay calls the same functions. Run `pytest -q` on the whole suite; copy `config/bot_config.json` before and restore
it after; commit no change to it.

## Do not

- change `ob_observe.py`, `tools/order_blocks.py` or any prereg;
- turn anything on in `config/bot_config.json`;
- fetch data or run against Alpaca.

## Hand-back

Push the branch and reply with the commit hash, the test count, the resolutions, and anything in the prereg you found
ambiguous.
