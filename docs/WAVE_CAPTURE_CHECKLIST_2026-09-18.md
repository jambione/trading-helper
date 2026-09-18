# Wave capture checklist — 2026-09-18

**Goal:** Ride lively morning tape on the *right* names — still-in-the-move momentum, not blow-offs — with all-day multi-open occupancy and small consistent capture.

**North star (product):** same energy as Fri morning’s movement, green MFE that actually pays. Do **not** loosen RSI/EXH arm gates or raise position ceiling mid-session to “fix” capacity.

**Live baseline (mini `55405c7`, post-restart):**
- Arm-ready admit + unarmable eviction 90s + CHG prefer ~8–40% (soft max 50%)
- RSI rising + soft-cap 75 · EXH rising · heat_max=0
- Slip/spread gates · `ai_entry_order_style=market`
- Trail give 0.35R · time-decay on · overtake off · BE @ 0.15R + $0.01 · min-hold 90s
- Zone=pullback · edge=continuation · max_positions=5

---

## After close / next lively session — do in order

### 1. Prove admissions (scoreboard first — no knob chase)
On the next lively morning (or full RTH if tape is quiet), score **prefer-band** opens vs **outside**:

| Metric | Prefer CHG 8–40% | Outside / >soft max |
|--------|------------------|---------------------|
| n opens / closes | | |
| win rate · $ · sum R | | |
| % with MFE ≥ 0.15R | | |
| never_green (MFE ≤ 0) | | |
| med hold | | |

Also: open-minutes (≥1 / ≥2 / ≥3 concurrent). **Pass signal:** prefer-band pays; blow-offs / outside don’t; book not flat for long stretches.

If this fails → go to §2. If it passes → hold knobs; only occupancy/sizing left.

### 2. Catch the wave earlier (admissions-only, only if §1 misses runners)
- Soft-seed / rank bias toward mid-band CHG **~8–20%** (still rising RSI/EXH).
- Faster never-armable eviction if seats go stale without arming.
- **Locked:** RSI/EXH arm gates, heat_max, max_positions.

### 3. True market fills end-to-end
Confirm every entry/exit path is real market (no marketable-limit TTL that skews slip/MFE).
- Ref: `scratchpad/MARKET_ENTRY_AFTER_CLOSE_2026-09-17_CLI_BRIEF.md`
- Verify with fill ledger (`ai_reports/fills/`) + outcomes `entry_slippage_r`.

### 4. Size from free equity
Set / bake `ai_size_from_free_equity=true` (and confirm slot math): ~3–4 concurrent slots, **larger** share counts from free equity — not fewer seats, not tiny 1-share tickets.
- Live config was unset (`None`) at checklist write — flip after close, restart, spot-check `size_plan` rows.

### 5. Occupancy KPI (first-class)
Thin/dead book = fail even if single-name scalp is green.
- Use open-minutes scoreboard (`feat/bench` already on master-mac).
- Target: multiple overlapping opens through the day; minutes with open≥2–3 matter as much as P&L narrative.

### 6. Hold the line mid-session
- No looser RSI/EXH arms.
- No higher `ai_max_positions` to paper over empty seats.
- Capacity fixes = admit/seed/evict/tape only.

---

## Done when
1. Prefer-band names show higher MFE≥0.15R rate and better $ than outside on a lively day.
2. Open-minutes ≥2 looks like a fed book, not a revolving single name.
3. Fill path is market-clean; sizing uses free equity.
4. Next lively morning is “movement we love” *and* green — without touching arm gates.

## Explicit non-goals
- Mid-session arm loosening
- Re-enabling overtake chase
- Raising daily loss / position ceiling as a capacity hack
- Premarket Phase B scope creep (own lane; score separately)

---

## 7. EXH arm: require dual-%R tight (SMCI yes / RKLB no) — locked 2026-09-18

**Operator rule:** Buy when both %R lines are elevated and close (OB + tight). Do **not** arm on fast-only heating with a wide gap.

| | SMCI (buy) | RKLB (refuse) |
|--|------------|---------------|
| Picture | Both lines up top, together, red-box | Fast climbing alone, slow lags |
| Live example | fast≈−13 · slow≈−4 · gap≈9 · ob+tight | fast≈−39 · slow≈−64 · gap≈25 · not tight |
| Heat | ~87% with confluence | ~61% from fast alone |

**Change (after close — not mid-session):**
- Bind `last_heating` / arm pass so it requires `tight` (gap ≤ `rte_confluence_max`, default 15) **or** refuse when `gap > 15` / `pctr_tight=false`.
- Keep slow line in the gate (`pctr_slow` present); heat % may stay fast-only for display.
- `ai_watch_tv_exh_rsi` is currently false — either enable the dual-line path for arms or teach `last_heating` the same tight check.
- **Locked:** do not loosen RSI soft-cap / rising or EXH rising to compensate.

**Done when:** SMCI-class still arms; RKLB-class `last_heating` refuses with a clear reason (`exh_not_tight` / wide gap).
