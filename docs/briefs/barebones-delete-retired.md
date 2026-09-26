# Brief: barebones step 3 — delete retired arms and dead settings

For a cloud session. Everything needed is in this repo; the session cannot
reach the Mac mini, the live desk or Alpaca, and does not need to.

**Repo:** jambione/trading-helper. It is PUBLIC: never commit secrets, keys,
tokens or account numbers.
**Start from:** branch `barebones-freshness`. **Create:** branch
`barebones-delete-retired`. Push that branch when done. Do NOT merge into any
other branch and do NOT open a PR.

## Goal

Remove retired entry-arm code paths and dead settings **without changing live
behaviour**. The live config is `config/bot_config.json` (tracked in git,
identical to what the live desk runs). Defaults live in `config.py`. The main
decision code is `ai_entry_watch.py` (~18k lines); exits are in
`ai_positions.py`.

## What to remove

Verify each item against `config/bot_config.json` AND the `config.py`
defaults before deleting. Only delete a path whose switch is off in BOTH, so
that deleting it cannot change behaviour.

1. **Retired arms, currently off:** the square arm (`ai_watch_exh_square_arm`,
   `ai_watch_exh_heating_with_square`, `ai_watch_square_max_age_sec`), the
   pre-square threshold (`ai_watch_exh_pre_thr`) if only used by retired arms,
   the oversold triangle arm (`ai_watch_exh_oversold_triangle_arm`,
   `ai_watch_os_triangle_max_age_sec`), any sticky dual-%R square latch used
   only by those arms, and `legacy_arms.py` once nothing references it.
2. **MACD gap arm and MACD arm gates, currently off:** `ai_watch_macd_gap_arm`,
   `ai_watch_macd_gap_min_pct`, `ai_watch_macd_gap_rsi_max`,
   `ai_watch_macd_block_narrowing`, `ai_watch_macd_exh_override`,
   `ai_watch_macd_exh_override_min_pct`, `ai_watch_arm_require_macd` — only
   where they are entry-arm logic. Do NOT touch MACD used by exits
   (`ai_exit_macd_*`) or anything in `ai_positions.py` exits.
3. **RSI arm gates that are off:** `ai_watch_arm_require_cm_rsi`,
   `ai_watch_arm_cm_rsi_require_rising`, and related `ai_watch_arm_cm_rsi_*`
   knobs only if their current values make them no-ops (e.g. a max of 100 or an
   allow-falling-below threshold that never binds). If a knob is not provably a
   no-op, keep it and say why.
4. **Settings with no reader outside config.py:** at least `cm_rsi_prefer_green`
   and `rsi_sell`. Re-check with a repo-wide search including `tools/` and
   `tests/`.
5. **`ai_watch_admit_prefer_square`** (off) and its eviction path, if that path
   is only reachable when the switch is on.

## How

- For every deleted config key, also remove it from:
  - the `config.py` defaults dict;
  - any key lists in `config.py` (e.g. the typed/coerced key list near the other
    `ai_watch_*` names);
  - the `learn_stamps.py` fingerprint lists;
  - `tools/setup_audit.py`;
  - `config/bot_config.json`.

  Keep config files valid JSON.
- Where live code reads a deleted switch as `cfg.get(key, default)` and the
  branch is dead, delete the branch and simplify. The live path must stay
  equivalent in behaviour.
- **Tests:** run `python -m pytest -q tests`. Install dependencies first if
  needed: alpaca-py, pandas and numpy (see `requirements.txt` if present).
  - Baseline is about 3,695 passed / 1 xfailed; the suite must end fully green.
  - Delete or update tests that ONLY exercise removed features; never weaken a
    test of a surviving path.
  - `tests/test_setup_audit.py` and `tests/test_desk_io_boundary.py` must pass.
- Make small commits, one per group above. Each message should say what was
  removed and why it cannot change behaviour, and end with the line:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Do NOT touch

- **Exits:** `ai_positions.py` exit logic (trail, ratchet, left_overbought,
  dead_trade, EOD flatten).
- **Entry safety gates:** price band, spread caps, max positions, loss brake.
- **The live mid-rise arm:** `ai_watch_exh_mid_rise_arm` /
  `_mid_rise_allows_buy`.
- **Recording and replay:** `desk_io.py`, `session_recorder.py`,
  `tools/replay_session.py`, `tools/nightly.py`.
- **The freshness code:** `cross_checked_quote`, `price_src_fresh`,
  `ai_watch_quote_freshness`, and the price-clock stamping.
- Anything under `tools/studies/`.

## Report back

- The branch name and commit SHAs.
- Each item removed, with the lines deleted.
- The settings count before and after: the union of keys in the `config.py`
  defaults and `config/bot_config.json`. It was 614 on 2026-09-26.
- Anything you chose NOT to remove, and exactly why.
- The final test result line.
