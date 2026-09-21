# CLI brief addendum — Square-ready admit gap + false ■ on entry (2026-09-21 PM)

**Repo:** `jambione/trading-helper` · base `master-mac`  
**When:** **after close** (no mid-session arm loosen). Optional: pause paper opens now if TV↔engine ■ mismatch continues.  
**Extends:** `docs/PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md` (farm shipped `a9583ea`; mix still far-heavy).

## Evidence

1. **Book mix not square-ready** (~12:56 ET): ~8 seats · **7 far · 0 pre_square · 0 square** · funnel `warming_n=0` despite `max_far=0` / farm knobs live.
2. **PSKY entered twice** (12:42, 12:55) with journal `arm_why=square` / `exh_seat_class=square` and fast `pctr` in OB band (−17.5 / −15.1), but:
   - first fill `exh_seat_class_admit=far`
   - features **omit `pctr_slow` / gap** — dual+tight not auditable
   - operator TV showed **no ■**
3. Entry path still uses **`dual_r_ob_tight(..., sticky=True)`** in `_square_exh_allows_buy` — cached `pctr_ob` / `pctr_tight` **OR** live math. That OR-latch can label a buy `square` after TV has already left dual OB+tight (same class of bug as MARA exit lag, but on **entry**).

## Product locks

- Enter = live dual-%R OB+tight only (TV red ■). **No sticky cache on entry.**
- Prefer seating `pre_square`/`square`; far starved. Arms / confluence / threshold unchanged.

## Implement

### A. Entry square = live dual only (kill sticky)

1. `_square_exh_allows_buy`: call `dual_r_ob_tight(record, cfg, sticky=False)` (or remove sticky arg on entry).
2. Refuse when fast or slow missing (`no_exhaustion_data`) — no single-line / cache-only ■.
3. Tests: cached `pctr_ob=True` but live fast/slow left OB → **refuse**; both live OB+tight → allow; wide gap → refuse.

### B. Journal proof at fill

On every buy outcome/features: `pctr`, `pctr_slow`, `pctr_gap`, `pctr_ob`, `pctr_tight`, `arm_why`, `exh_seat_class`, `exh_seat_class_admit`, `sticky_used=false`.

### C. Pre_square first-class keep (admit gap)

1. Under `ai_watch_admit_prefer_square`: **`pre_square` and `square` take keep seats** even when `admit_require_arm_ready` is on — arm_ready remains required to **open**, not to **keep** an approaching seat.
2. Non-ready far → scout/steal only; do not pin the book.
3. Ensure dual-%R is stamped on watch rows every poll so class isn’t null→far.
4. Board: `n_square` / `n_pre_square` / `n_far` must reflect reality.

### D. Ship

After close → PR → pull mini → restart. Paper only. Score next RTH: zero buys with TV missing ■; book mix majority pre_square/square when candidates exist.

## Explicit non-goals

- Loosen `rte_threshold` / confluence / square arm.
- Re-enable heating-only / `last_heating` entry.
- Mid-session restart unless Jonathan pauses desk first.

## Suggested commits

- `fix(arm): entry square uses live dual-%R only (no sticky OR-latch)`
- `feat(watch): pre_square keep under prefer-square even when arm_ready gate on`
- `chore(journal): pctr_slow+gap on every square fill`
