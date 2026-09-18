# CLI brief — Square-aligned watch admit / bus

**Repo:** jambione/trading-helper · base `master-mac`  
**Locked with Jonathan 2026-09-18.**

## Context

EXH product is live: **enter on dual-%R OB+tight (red ■)**, exit on leave-OB (▼). RSI arm gate off. Trail = backup (time-decay off, arm ≥0.25R).

Problem: watch seats are filled with momentum/movers/trending names that are **far from square**. Snapshot 2026-09-18 ~14:48 ET: **8/8 seats far** (wide gap and/or both lines not near OB). Square arm correctly refuses them → thin opens. **Do not loosen square arms.** Fix the **admit / soft-seed / eviction bus** so seats are pre-square or square.

## Product rule (admissions)

| Seat quality | Meaning | Action |
|--------------|---------|--------|
| **square** | both %R ≥ −`rte_threshold` (20) AND `|fast−slow| ≤ rte_confluence_max` (15) | Prefer keep / pin; armable |
| **pre-square** | both lines approaching OB (e.g. both ≥ −35 or −40) AND gap ≤15 (or closing) AND EXH rising | Soft-seed / warming OK |
| **far** | deep OS, one-line only, or gap ≫15 | Do not consume keep seats; fast eviction |

Locked arm gates stay: `ai_watch_exh_square_arm`, dual OB+tight, EXH rising, no RSI arm require.

## Implement

### 1. Square-distance score (shared helper)

Add a small helper (e.g. in `ai_entry_watch.py`) used by admit + seed rank + eviction:

```text
inputs: pctr (fast), pctr_slow, pctr_rising (optional)
square: both >= -thr and gap <= confluence
pre_square: both >= -pre_thr (new knob, default 35) and gap <= confluence
            and (pctr_rising or slow rising if available)
far: else
gap = abs(fast - slow)
```

Expose on watch rows / events: `exh_seat_class` = `square|pre_square|far`, plus `pctr_gap`.

### 2. Soft-seed / admit prefer pre-square

When `ai_watch_admit_require_arm_ready` (or new `ai_watch_admit_prefer_square=true` default on):

- Soft-seed / keep inclusion: **prefer** `pre_square` or `square` over raw score/CHG alone.
- Rank boost for square/pre-square; demote `far`.
- Optional hard gate for *new* soft-seeds: refuse `far` when book already has ≥N far seats (`ai_watch_max_far_exh_seats`, default 2) — mirrors stale-tape seat cap.
- CHG prefer band (8–40%) stays as soft preference, secondary to square-distance.
- Research/warming seats may still enter as scouts with short TTL, but must not block pre-square steals.

### 3. Never-square eviction (bus)

Extend never-armable / unarmable steal:

- If seated name stays `far` for `ai_watch_far_exh_evict_sec` (default 90–120s) with young tape available, evict / steal for a `pre_square` or `square` candidate.
- Reason: `far_exh` / `never_square` (log + admit ledger).
- Pins: either subject to same rule or only demote when far *and* not stream-printing — pick one; prefer **pins can be stolen when far** so the book doesn’t freeze on HOOD/ONON-class lag.

### 4. Warming band = pre-square

Retarget warming / scout EXH band to pre-square window (both lines in approach band, gap tight), not only `heat_min` on fast alone.

- Update `warming_exh_band` / preheat logic to use dual-line class when slow is present.
- Missing `pctr_slow`: treat as unknown — do not count as pre-square; allow short scout TTL then drop.

### 5. Config (bake defaults)

```json
"ai_watch_admit_prefer_square": true,
"ai_watch_exh_pre_thr": 35.0,
"ai_watch_max_far_exh_seats": 2,
"ai_watch_far_exh_evict_sec": 90.0
```

Also bake today’s live mid-session knobs if not already in repo defaults:

- `ai_watch_arm_require_cm_rsi=false`
- `ai_local_trail_time_decay_enabled=false`
- `ai_local_trail_arm_r=0.25`

Do **not** change: `rte_threshold`, `rte_confluence_max`, `ai_watch_exh_square_arm`, square arm math.

### 6. Tests

- Fixture far (RKLB-like gap 25, not OB) → not preferred for soft-seed; evict after TTL.
- Fixture pre-square (both ~−30, gap 8, rising) → admit/keep preferred.
- Fixture square (SMCI-like −13/−4) → highest prefer; arm still passes existing square tests.
- Book with 3 far + 1 pre-square candidate → steal/evict toward pre-square when cap exceeded.

### 7. Ship

Branch off `master-mac` → PR → merge. After merge: pull mini, restart (admit path is code). Spot-check watch: majority seats `pre_square`/`square` within 1–2 soft-seed cycles.

## Done when

1. Soft-seed / keep no longer fills the book with all-far names when pre-square candidates exist.
2. Far seats evict on TTL; logs show `far_exh` / `never_square`.
3. Square arm unchanged (SMCI yes / RKLB no).
4. Live watch mix shifts toward pre-square/square without loosening OB+tight.

## Explicit non-goals

- Loosening square arm (threshold / confluence / dual OB).
- Re-enabling RSI as arm gate.
- Re-enabling trail time-decay / arm_r=0.
- Premarket Phase B.
- Raising max_positions to fake capacity.
