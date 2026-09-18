# CLI brief — EXH square / triangle mode (enter + exit)

**Repo:** jambione/trading-helper · base `master-mac`  
**Locked with Jonathan 2026-09-18 from TradingView %R Trend Exhaustion [upslidedown].**

## Product (sole thesis)

| TV | Meaning | Desk action |
|----|---------|-------------|
| First red ■ / OB starts | Enter | Arm when **both** fast+slow %R ≥ −`rte_threshold` (default 20) **and** tight (`|fast−slow| ≤ rte_confluence_max`, default 15) |
| Squares keep printing | Hold | Stay long while dual-line `overbought` true |
| Red ▼ triangle | Exit | Flatten on `ob_reversal` (was OB, now not) → `left_overbought` |

- **Not** the thesis: RSI arm gate, `last_heating` alone (fast-only heat), trail-as-primary exit.
- Trail / BE / min-hold remain **backup leash** only (capital protection), not the reason we exit a good square run.
- Do **not** loosen RSI/EXH mid-session for capacity; this *replaces* the arm story with dual-%R OB+tight.

## Current gaps

1. `exhaustion_allows_buy` can return `heating` / `overbought` without requiring tight → RKLB-class arms.
2. `ai_watch_tv_exh_rsi=false` so `_tv_exh_rsi_allows_buy` (already has tight) is not the main path.
3. `ai_exit_left_overbought=false` on scalp_legacy → triangle exit is off; book uses local_trail instead.
4. `ai_exh_falling_flatten_enabled=false` (keep off unless it matches ▼; prefer true leave-OB).

## Implement

### A. Enter = square (OB + tight)

In `ai_entry_watch.exhaustion_allows_buy` (scalp_legacy path when `tv_exh_rsi` off):

1. Before any `return True, "heating"` or `"overbought"` (and hot/macd_armed siblings that mean “in the square”):
   - Require `pctr` + `pctr_slow` present (else `no_exhaustion_data` / refuse).
   - Compute `gap = abs(fast - slow)`; `tight = gap <= rte_confluence_max` (or `pctr_tight`).
   - Require **both** `fast >= -rte_threshold` and `slow >= -rte_threshold` (dual OB = square).
   - Require `tight`; else `return False, "exh_not_tight"`.
2. Prefer **not** arming on mid-band “heating” unless both lines are already in the OB band (square). Optional: drop pure `heating` pass entirely for this mode — enter only on dual OB+tight (`overbought` / `last_overbought`).
3. Config flag (recommended): `ai_watch_exh_square_arm=true` (default true once shipped) so the behavior is explicit and testable. When false, preserve prior heating path for rollback.
4. Keep `ai_watch_require_exh_rising` as-is for now (rising into the square is fine); do **not** re-tighten RSI. Optionally set `ai_watch_arm_require_cm_rsi=false` only if Jonathan confirms RSI fully off for this mode — **default leave RSI knobs unchanged** unless tests show they block square arms; document either choice in the PR.

### B. Exit = triangle (leave OB)

1. Set / bake `ai_exit_left_overbought=true` for scalp_legacy in `config/bot_config.json` + `config.py` default.
2. Ensure `left_overbought` fires on dual-line leave-OB (`ob_reversal` semantics: was both-OB, now not) — align with `signals.compute_percent_r_exhaustion` / live `pctr_ob` edge, not fast-only heat drop.
3. Confirm min-hold does not eat the triangle forever; short defer OK, but ▼ must still exit.
4. Leave `ai_exh_falling_flatten_enabled=false` unless leave-OB alone misses the triangle; do not re-enable `no_progress`.

### C. Tests

- SMCI-like: fast≈−13, slow≈−4, gap≈9, both OB → arm pass (`overbought` / square).
- RKLB-like: fast≈−39, slow≈−64, gap≈25 → refuse `exh_not_tight` (no arm).
- Leave-OB: prior both-OB then break → `left_overbought` due.
- Still-OB (squares): no left_overbought.

### D. Ship

- Branch off `master-mac`, PR, merge.
- After merge: pull mini, restart stack (needed for arm/exit code).
- Marker note in scratchpad or outcomes fingerprint for post-change scoreboard.

## Done when

1. Only dual-OB+tight names arm (squares).
2. Open positions flatten on leave-OB (triangle), not only trail scrapes.
3. RKLB-class cannot `last_heating` in.
4. PR merged; mini restarted; one live refuse + one live left_overbought (or paper sim) logged.

## Explicit non-goals

- Mid-session knob spray without restart after code land.
- Loosening RSI soft-cap / max_positions for capacity.
- Premarket Phase B scope.
- Re-enabling no_progress / overtake chase.
