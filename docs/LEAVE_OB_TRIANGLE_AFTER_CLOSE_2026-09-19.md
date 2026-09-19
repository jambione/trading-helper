# Leave-OB triangle freshness — shipped marker

**Root cause (MARA 2026-09-18):** manage loop refreshed **fast %R only**; `dual_r_ob_tight` OR-latched sticky `pctr_ob`, so stale slow/`pctr_ob` kept `overbought_hold` after TV ▼. MARA `left_overbought` at **15:39:38 ET** still logged fast `pctr=-17.86` (still in −20 OB band). APLD held to EOD (flicker ▼→■ was correct; no confirm window yet).

**Fix:** `apply_live_exhaustion` (dual) on every exit check; `both_ob` from live math only; `ai_exit_left_overbought_confirm_sec=3` flicker guard; stale slow past `ai_exit_dual_slow_max_age_sec` + fast left → triangle.

| | |
|--|--|
| **Commit title** | `fix(exit): dual leave-OB triangle without stale slow lag` |
| **Score** | Compare post-change `left_overbought` timestamps vs TV ▼ on next RTH; expect confirm≪30s, not multi-minute trail lag. |
