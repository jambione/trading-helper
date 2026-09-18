# After-close: EXH arm requires dual-%R tight

Locked with Jonathan 2026-09-18 from TV visuals: SMCI = buy, RKLB = don’t.

## Intent
`last_heating` must not arm when only the fast %R is heating and `|fast−slow| > rte_confluence_max` (15). Require OB+tight (both lines) for that path — same picture as red-box confluence on %R Trend Exhaustion.

## Locked
- RSI rising + soft-cap &lt;75
- EXH rising / heat_min
- No mid-session ship unless asked

## Implement
1. In arm recheck / `last_heating` (and any sibling that arms on heating without OB), require:
   - `pctr` and `pctr_slow` present
   - both ≥ −`rte_threshold` (OB), **or** keep heating band but add:
   - `abs(pctr - pctr_slow) ≤ rte_confluence_max` (tight)
2. Prefer refuse reason `exh_not_tight` (already used by TV EXH path).
3. Optional: set `ai_watch_tv_exh_rsi=true` only if it doesn’t fight scalp_legacy; otherwise patch `last_heating` in place.
4. Tests: fixture SMCI-like (gap 9, both OB) → pass; RKLB-like (gap 25, not OB) → refuse.
5. Bake config defaults; restart mini after close.

## Verify
Log one refuse with gap on a wide-gap heater; confirm SMCI-class still `entry_ok`.
