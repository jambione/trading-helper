# CLI brief — Leave-OB / triangle lag (dual-%R exit)

**Repo:** `jambione/trading-helper` · base `master-mac`  
**Why:** Friday 2026-09-18 — TV printed ▼ / left squares earlier than the desk. **MARA** stayed open until `left_overbought` ~15:39 (lag). **APLD** brief ▼ then back to ■ — hold was correct (flicker). Product thesis: exit on dual leave-OB (triangle), trail is backup only.  
**Priority:** before / right after Monday Phase 1 so exit timing is scoreable.

## Product

| TV | Desk |
|----|------|
| Red ■ dual OB+tight | Hold (`overbought_hold`) |
| Red ▼ leave dual OB | Flatten `left_overbought` **promptly** |
| Flicker ▼ then ■ again | Stay long (do not scratch on one-tick noise) |

- Enter path is already square-mode; this brief is **exit freshness only**.
- Do **not** re-enable `no_progress` or loosen square arms.

## Current behavior (hypothesis — verify, don’t assume)

`exhaustion_exit_now` (dual branch):

1. `both_ob, _, err = dual_r_ob_tight(record, cfg)`
2. If `both_ob`: set `exh_was_overbought`, return hold
3. If was OB and now not: return `left_overbought`

`dual_r_ob_tight` treats OB as:

```text
both_ob = bool(ind["pctr_ob"]) OR (fast >= -thr AND slow >= -thr)
```

**Suspects for MARA-class lag**

1. **Stale `pctr_slow` / `pctr_ob` on the open position** — position loop refreshes fast `%R` more eagerly than the slow line; slow stays “OB” → `both_ob` stays true → triangle never fires while TV already left.
2. **`pctr_ob` OR-latch** — if `pctr_ob` can stay true from a stale dual read while live fast+slow math already left OB, the `or` keeps hold.
3. **Min-hold / exit defer** swallowing ▼ (short defer OK; must not hold through a real leave).
4. **Missing slow on open** → `no_exhaustion_data` → no exit (trail-only) — wrong for square mode once we had dual OB at entry.

**APLD flicker** must remain hold: require leave to **persist** N ticks/seconds (confirm), not instant one-print exit — without reintroducing MARA multi-minute lag.

## Implement

### A. Prove the lag on tape (before coding if logs handy)

1. Pull MARA (and any peer) open→close from 2026-09-18: timestamps for TV ▼ vs desk `left_overbought` / trail.
2. Dump per-poll `pctr`, `pctr_slow`, `pctr_ob`, `pctr_tight`, `exh_was_overbought`, `exhaustion_exit_now` reason while open.
3. Write one sentence root cause into the PR (stale slow vs OR-latch vs min-hold vs poll cadence).

### B. Fresh dual read on every exit check

1. In the **position / manage loop**, before `exhaustion_exit_now`, refresh **both** fast and slow `%R` from the same path used at arm (stream/engine pair), not fast-only.
2. If slow cannot be refreshed: fail closed for *new* dual latching; if `exh_was_overbought` already true, prefer exit on **fast leave-OB + slow missing/stale past max_age** *or* keep hold only for `slow_max_age_sec` then exit — pick one, document, test. Default recommendation: **stale slow past N sec while fast has left OB → `left_overbought`** (triangle-first, not trail-first).
3. Stop trusting a sticky `pctr_ob` alone for hold: in dual mode compute  
   `both_ob = (fast >= -thr and slow >= -thr)`  
   and treat `pctr_ob` as optional cache only if it matches that formula.

### C. Flicker guard (APLD)

1. Add confirm: leave-OB must hold for `ai_exit_left_overbought_confirm_sec` (start **2–5s**) or 2 consecutive dual reads before flatten.
2. If dual OB returns inside the confirm window → cancel pending exit (squares back on).
3. Cap confirm so MARA-class multi-minute lag cannot return (confirm ≪ 30s).

### D. Min-hold

1. Audit min-hold vs `left_overbought`: allow triangle to override after a short floor, or log `left_overbought_deferred` with remaining sec.
2. Trail remains backup; when triangle is due, do not wait for trail arm.

### E. Tests

- Dual OB → both leave → `left_overbought` within confirm window.
- Fast leaves, slow still OB → hold (still dual OB).
- Fast leaves, slow **stale** past max_age + was OB → exit (MARA class).
- Leave one tick then dual OB again inside confirm → hold (APLD class).
- Square mode off → legacy fast-band path unchanged.
- No `no_progress` / trail knob changes.

### F. Ship

- Branch off `master-mac`, PR, merge.
- Pull mini, **restart** (exit path in ai_entry_watch).
- Prefer land **before Monday open** or **after Monday close** — avoid mid-session restart on Phase 1 score day.
- Marker in outcomes / scratchpad for post-change exit timing vs TV.

## Done when

1. Root cause named from a real open (MARA-class) in the PR.
2. Dual leave-OB fires on fresh dual math (± small confirm), not minutes later via trail.
3. APLD-class flicker still holds.
4. Suite green; mini restarted outside RTH or after the Phase 1 day.

## Explicit non-goals

- Loosening square enter / RSI / heat_max / max_positions.
- Re-enabling `no_progress` or exh_falling as primary exit.
- Phase B / SIP / fractional work in this PR.
- Live arming.

## Suggested commit title

`fix(exit): dual leave-OB triangle without stale slow lag`
