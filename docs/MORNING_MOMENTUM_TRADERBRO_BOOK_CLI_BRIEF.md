# CLI brief — Morning book: every momentum + every Trader Bro suggestion (09:30–11:00 ET)

**Repo:** `jambione/trading-helper` · base `master-mac`  
**Locked with Jonathan 2026-09-19:** From **9–11** (RTH morning: **09:30–11:00 ET**), the AI Watch book must carry **every momentum name** and **every Trader Bro (AI research) suggestion** — seating/occupancy, not looser arms.

## Why

Fri 2026-09-18 morning &lt;11 was the −6R hole while the book was thin later in the day on occupancy. Product fix = **feed the book** with the two sources Jonathan trades by eye (Momentum panel + Trader Bro / research boards), not weaken square OB+tight.

## Product

| Window (ET) | Book policy |
|-------------|-------------|
| **09:30–11:00** | Seat **all** current momentum-flagged / mom-open / big-mover momentum names **and** **all** rows from Trader Bro research boards (Grok + AGY/Claude suggestions + live seed_rank watch-facing). Soft-seed / keep must not drop them for prefer-square or soft_seed_max. |
| **11:00+** | Resume normal prefer-square admit bus + soft_seed caps (no all-day flood). |

- **Arms unchanged:** dual OB+tight square · RSI rising-only · trail backup. Soft-seated morning names still need the square to open.
- **Not** this brief: loosening `heat_max`, raising `max_positions`, or arming on heating alone.

## Current gaps

1. `ai_watch_soft_seed_max=12` caps the soft-seed batch — cannot be “every.”
2. Momentum seed N caps (`ai_watch_seed_momentum_n=12`, `…_open_n=14`, `momentum_max_tickers=10`) truncate the panel.
3. Research seed N (`ai_watch_seed_research_n=12`) truncates Trader Bro boards.
4. Prefer-square / far-exh eviction can boot pre-square momentum/research seats during the morning spray when we most need them watched.
5. No time-of-day switch for “morning flood” vs afternoon square-prefer bus.

## Implement

### A. Config — morning flood window

```text
ai_watch_morning_flood_enabled: true
ai_watch_morning_flood_start: "09:30"   # ET
ai_watch_morning_flood_end:   "11:00"   # ET
```

Optional: `ai_watch_morning_flood_include_pre: false` (keep false unless Jonathan wants 09:00–09:30 too).

### B. While flood active (`_et_hour_decimal` in [start, end))

1. **Momentum:** pull the full momentum desk list (flagged + open-seed path + big-mover helpers already used by soft-seed / open seed) with **no N truncate** (or N ≥ panel size, e.g. 50). Still skip levered ETPs / non-tradable.
2. **Trader Bro:** `research_candidate_rows()` — seat **every** returned symbol (no `ai_watch_seed_research_n` slice). Boards: `grok_suggestions.json`, `agy`/`claude` suggestions, watch-facing `seed_rank_*`.
3. **soft_seed_max:** bypass or set effectively unlimited for momentum + research sources during flood (other sources may keep a modest cap).
4. **Prefer-square / far eviction:** do **not** evict or refuse-keep a seat whose source is momentum* or research/xai/agy solely for `far` / prefer-square during flood. Still allow tape-dead / stale eviction.
5. **arm_ready admit:** soft seats may stay without arm_ready; **opens** still require square + existing arm gates.
6. Log `morning_flood=1` on seed/keep for scoreboard.

### C. After 11:00

Restore prefer-square soft-seed, `soft_seed_max`, and source N caps. Existing far-exh / prefer-square bus unchanged.

### D. Tests

- 10:00 ET + 20 momentum + 15 research → all 35 attempt seat (minus levered/dupes); soft_seed_max does not clip them.
- 11:30 ET → soft_seed_max and N caps apply again.
- Square arm still refuses non-OB+tight morning seats.
- Flood off → behavior identical to today.

### E. Ship

- Branch off `master-mac`, PR, merge.
- Pull mini, restart before Monday open (or Sun evening).
- Monday Phase 1: expect higher open≥1 minutes in the morning; do **not** retune arms mid-session.

## Done when

1. 09:30–11:00 watch shows the full momentum panel ∪ full Trader Bro suggestion set (practical: ledger/seed logs list them with `morning_flood`).
2. After 11:00 prefer-square bus returns.
3. Arm gates untouched; suite green; mini restarted outside RTH.

## Explicit non-goals

- Arming every flooded name.
- Changing square/triangle exit.
- Phase B / SIP.
- Raising all-day soft_seed_max without the time gate.

## Suggested commit title

`feat(watch): morning flood — all momentum + Trader Bro seats 09:30–11`
