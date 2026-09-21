# CLI brief — Aggressive pre-square farm + trail/triangle exit race + no-extension after ■

**Repo:** `jambione/trading-helper` · base `master-mac`  
**When:** **after close only** (Mon 2026-09-21 Phase 1 score day — no mid-session restart / no arm loosen).  
**Evidence (~morning 2026-09-21):** enter OK (12/12 dual-%R OB+tight, zero `last_heating`); exit owned by `local_trail` (8) vs `left_overbought` (2) vs fill-through (2); 8/12 never MFE≥0.15R; after open burst book far-heavy (few/no `pre_square` seats). Prefer-square/warming parked some names near ■, but we are **not** aggressively farming the pre-square band.

**Related (do not redo blindly):**
- `docs/SQUARE_ADMIT_BUS_CLI_BRIEF.md` — prefer-square admit (shipped; tighten/farm harder)
- `docs/MORNING_MOMENTUM_TRADERBRO_BOOK_CLI_BRIEF.md` — 09:30–11 flood (shipped)
- `docs/LEAVE_OB_TRIANGLE_LAG_CLI_BRIEF.md` + `docs/LEAVE_OB_TRIANGLE_AFTER_CLOSE_2026-09-19.md` — dual leave-OB freshness (shipped)
- Living list: `docs/PHASE1_AFTER_CLOSE_2026-09-21.md`

## Product locks (unchanged)

| | |
|--|--|
| Enter | dual-%R OB+tight square only (`ai_watch_exh_square_arm`) — refuse heating / wide gap |
| Exit | leave-OB triangle (`left_overbought`) primary; trail = **backup leash only** |
| Admit | prefer `square` / `pre_square`; far is a budgeted scout at most |
| Arms | do **not** loosen `rte_threshold` / `rte_confluence_max` / square arm / RSI rising gate / `heat_max` / `max_positions` |
| Scope | seating + exit priority / freshness + measurement — not “more opens via weaker arms” |

## Name collision — read this first

| Knob / term | Means | Not |
|-------------|-------|-----|
| **`pre_square`** (`exh_seat_class`) | both %R in approach band (≥ −`ai_watch_exh_pre_thr`, default 35) + tight gap + rising | a time-of-day window |
| **`ai_watch_morning_flood_include_pre`** | extend morning flood into **09:00–09:30** premarket | farming the pre_square EXH band |

Today’s hole is **`pre_square` seating**, not “turn on include_pre.” Leave `include_pre=false` unless Jonathan separately asks for premarket flood.

---

## Workstream 1 — Aggressively farm the pre-square band (P0 seating)

### Why

Prefer-square/warming exists, but the live book still went **far-heavy** after the open burst. Soft park ≠ farm. Goal: **majority seats `pre_square` or `square` all day**, with far quickly stolen when a pre-square candidate exists. Soft-seated names still need ■ to open.

### Current defaults (repo `config.py` — verify live mini override)

```text
ai_watch_admit_prefer_square: true
ai_watch_exh_pre_thr: 35.0
ai_watch_max_far_exh_seats: 2
ai_watch_far_exh_evict_sec: 90.0
ai_watch_morning_flood_enabled: true   # 09:30–11:00
ai_watch_morning_flood_include_pre: false   # keep false
```

### Implement

#### A. Harder far budget (all day, not only after 11)

1. Drop `ai_watch_max_far_exh_seats` default **2 → 0** (or **1** if zero starves brand-new dual-%R unknowns — pick 0 first; document if 1 needed).
2. When prefer-square is on: **refuse new soft-seed/keep of `far`** if any `pre_square`/`square` candidate is waiting (already partly true for keep pass — close gaps for soft-seed / morning flood sources).
3. Morning flood still seats all momentum ∪ Trader Bro (> $2), but **immediately** mark far flood seats stealable; do not let flood freeze a far-only book.

#### B. Faster far → pre-square bus

1. Cut `ai_watch_far_exh_evict_sec` **90 → 45** (or 30 if logs show pre-square candidates waiting behind far pins).
2. Ensure pins / flood sources are stealable when `far` past TTL (prefer-square product already leans this way — close any “flood immune forever while far” hole **except** tape-dead grace already documented).
3. Log steal reasons distinctly: `far_exh_steal_for_pre_square` / `far_exh_steal_for_square`.

#### C. Active pre-square hunt (the “aggressive farm”)

1. Soft-seed / reseat loop: from the **same** momentum + research (+ movers if already used) universe, **rank and pull `pre_square`/`square` first** every cycle — not only react when a seated name warms.
2. Raise admit/seed score gap: `square` ≫ `pre_square` ≫ everything; **far score floor** so far never outranks a true pre-square on CHG alone.
3. Warming ≡ dual pre_square/square only when prefer-square on (already intended in `ai_entry_watch`); audit any fast-only warming path that still seats heat-without-slow as soft keep.
4. Missing `pctr_slow`: **not** pre_square — short scout TTL then drop/steal (do not inflate pre_square counts).
5. Optional knob (only if needed after A–C): `ai_watch_exh_pre_thr` **35 → 40** to widen *approach seating* only — **never** change OB threshold / confluence used by square **arm**. Label clearly in PR.

#### D. Board / ledger so we can score the farm

1. Watch/board expose counts: `n_square` / `n_pre_square` / `n_far` (and % of seats).
2. Seed/admit ledger: class at seat time + class at steal/evict.
3. Phase 1 scorecard after change: target **pre_square+square ≥ ~60–70% of seats** in steady state outside the first 1–2 min of a flood spray; far spikes must mean-revert via bus.

#### E. Tests

- Book 6 far + 2 pre_square candidates → within one/two soft-seed cycles, far ≤ max_far and pre_square seated.
- Morning flood 20 mom + 15 research all >$2, mix of classes → all attempt seat, but far steals yield to pre_square/square ASAP; arms still refuse non-■.
- `pre_thr=35` fixture both −32 gap 8 rising → `pre_square`; both −13/−4 → `square`; gap 25 → `far`.
- Missing slow → not counted pre_square.
- Square arm unchanged (SMCI yes / RKLB-wide no).

---

## Workstream 2 — Exit race: trail vs triangle (P0 exit)

### Why

Morning closes: **trail 8 / left_overbought 2 / fill-through 2**. Product wants ▼ primary, trail backup. Leave-OB freshness already shipped (2026-09-19); Monday may be a **different** race: trail / BE path flattening **before** dual leave-OB, or while still dual OB after a dunk with little extension.

### Prove first (tape, then code)

For each morning `local_trail` close, dump at exit and T−30s:

- `pctr`, `pctr_slow`, `both_ob` / `exh_was_overbought`
- MFE_R / MAE_R, whether trail was armed, BE shelf, give
- Whether TV/engine showed ▼ yet
- Exit reason detail (`local_trail` subtype if any)

Write one root-cause sentence in the PR from real rows, e.g.:

- still dual OB at trail flatten → **trail overreached into square hold** (fix priority), or
- already left OB but triangle deferred → **confirm/min-hold/stale** (extend leave-OB brief), or
- never OB on manage path after fill → data bug.

### Implement (after root cause — pick matching slice)

#### A. Triangle-first while thesis holds

1. While `exh_was_overbought` and live dual still OB (fresh dual read): **do not** let trail/BE take the flatten that ends the trade — or only allow catastrophic MAE path (document threshold). Prefer hold-through dunks that stay dual OB+tight.
2. When live dual **leaves** OB (confirm window already 3s): `left_overbought` must win the race vs trail in the same manage tick (triangle does not wait for trail arm).
3. Keep APLD flicker: leave must persist confirm; cancel if ■ returns inside window.

#### B. Trail stays backup leash

1. Do **not** re-enable time decay.
2. Do **not** loosen square arms to “give room.”
3. If live mini uses `ai_local_trail_arm_r=0.15` (repo default is **0.25**; `be_at_r` default **0.15**) — document actual live fingerprint in PR; any trail knob change is **exit leash only**, after proof, and outside RTH.

#### C. Tests

- Dual OB hold + price dunk but still both OB+tight → no `local_trail` flatten (or only if MAE gate says so — assert chosen rule).
- Dual leave-OB confirmed → `left_overbought` even if trail shelf would also fire same tick.
- Flicker leave one print then OB → hold.
- Square mode off → legacy path unchanged.

---

## Workstream 3 — No-extension after ■ (measurement + seating leverage; no arm loosen)

### Why

8/12 opens never printed MFE≥0.15R → no extension after square; trail/triangle debate is downstream of dead follow-through. **Do not** fix by weakening OB+tight.

### Implement

1. **Journal / score fields** on every close: `mfe_r`, `mae_r`, `hit_trail_arm` (MFE≥ live arm_r), `bars_in_square_before_entry` (or time since dual first OB), `exh_seat_class` at admit and at fill, `arm_why` (square vs heating — stop logging only seed text).
2. Split closes into: **late-into-square** (entered after %R already extended) vs **dead follow-through** (entered early square, never ran).
3. Primary product lever for “late” is **Workstream 1** (be seated in pre_square so fill is near ■ start) — not arm changes.
4. No mid-session trail kill / no `no_progress` on.

### Done when (this stream)

- Post-close card can quote late-vs-dead counts.
- Next RTH can attribute whether pre_square farm improved MFE≥arm_r rate without arm loosens.

---

## Ship order (after close)

1. **Workstream 1** (pre-square farm) — highest leverage for occupancy + earlier ■.  
2. **Workstream 2** (trail vs triangle) — only after tape root cause.  
3. **Workstream 3** (journal/score) — can land with 1 or as small follow PR.

Branch off `master-mac` → PR → merge → pull mini → restart **outside RTH**. Paper only; live stay off.

## Done when (whole brief)

1. Steady-state book mix is mostly `pre_square`/`square`; far no longer parks the book after bursts.  
2. Named root cause for Monday trail-majority exits; triangle-first rule covered by tests when still-OB trail was the bug (or leave-OB path fixed if that was the bug).  
3. Closes expose MFE/late-vs-dead; arms unchanged; suite green; mini restarted after close.

## Explicit non-goals

- Loosening square enter / confluence / dual OB / RSI / heat_max.  
- Raising `max_positions` to fake capacity.  
- Enabling `ai_watch_morning_flood_include_pre` (premarket) unless separately requested.  
- Re-enabling `no_progress` or trail time-decay.  
- SIP / live / fractional in this PR.

## Suggested commit titles

- `feat(watch): aggressive pre-square farm — starve far seats`
- `fix(exit): triangle-first while dual OB; trail backup only` (after proof)
- `chore(journal): MFE + seat class + arm_why on closes`

## Paste-ready one-liner for Grok CLI

```text
After close on master-mac: implement docs/PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md
— (1) aggressively farm exh_seat_class pre_square/square all day (cut max_far, faster far_exh steal,
active pre-square hunt from mom+research; do NOT flip morning_flood_include_pre);
(2) prove then fix trail-vs-triangle race so left_overbought wins when dual leaves and trail
does not flatten while still dual OB; (3) journal MFE/late-vs-dead/arm_why. Arms locked.
Paper only, restart mini outside RTH.
```
