# Ratchet / local_trail stop study — 2026-09-24

**Question:** Is the local trail / peak-give ratchet protecting profit or cutting
runners? What exit set fits Jonathan's prefs (no no_progress; trail = backup
leash; left_overbought = thesis exit; EOD ~15:50)?

**Mechanism (code, measured):** `ai_positions.local_profit_stop` /
`apply_local_trail`.

1. At fill, seed a working shelf (`initial_local_stop`) from give × R.
2. Trail **arms** only after MFE ≥ `ai_local_trail_arm_r` (structure stop owns
   the open until then).
3. Once armed: shelf = max(prev, last − give, **peak × (1 − peak_give_pct/100)**),
   raise-only. Hit → flatten (`local_trail`).
4. Separate exits: `left_overbought`, EOD 15:50, stale_data, etc.

**Live knobs (mini `bot_config`, afternoon 2026-09-24):**

| Knob | Live | Role |
|---|---|---|
| `ai_local_trail_arm_r` | **0.06** (~+0.3% when R≈5%) | When trail starts chasing |
| `ai_local_trail_give_r` | 0.35 | Cushion under last, in R |
| `ai_local_trail_peak_give_pct` | **0.35** | Floor shelf at 0.35% under high-water |
| `ai_local_trail_be_at_r` | 0.15 | Breakeven floor after +0.15R |
| `ai_local_trail_entry_catchup_sec` | 0 | 30s catch-up off |
| `ai_no_progress_flatten_enabled` | **False** | Off (Wed had 5 fires) |

**When versions shipped (git log):**

| When ET | Commit | Change |
|---|---|---|
| ~Sep 18 | `125adee` | arm_r **0.15** |
| Wed 12:46 | `553e6e0` | never-green / no_progress-ish on |
| Wed 13:33 | `dc5bcce` | arm_r **0.15 → 0.06** (+0.3%) |
| Wed 16:13 | `b242e72` | peak ratchet feature + one arm |
| Wed 16:20 | `d28857b` | `peak_give_pct=0.35`; never-green **off** |
| Wed trail fixes | `65ec7d8`…`9ea491e` | catch-up, shelf under fill, ratchet while &lt;0.15R |

Era tags used in the study: `era_pre918` / `era_arm015` (9/18–22) /
`era_peak035` (9/23+).

---

## Data

- **414** closed trades since 2026-09-01 with SIP 1m bars in
  `runway_bars_cache.pkl` (offline; no API).
- Live reasons: local_trail 292, left_overbought 53, fill_through_stop 26,
  no_progress 12, dead_trade 14, …  
- Script: `tools/studies/ratchet_stop_study.py` →
  `ai_reports/ratchet_stop_study.json`.
- 1m bars **understate** tick-level chase; treat as directional, not exact P/L.

**Runner labels in CF:**  
- **saved** = trail/shelf exit, and later the path hit the *initial* stop  
  (ratchet avoided a worse stop-out).  
- **cut short** = trail exit, and later price reached **+0.35R above the exit**
  before hitting the initial stop (gave away a runner).

---

## Variant table (measured)

Net $ after a flat **0.20%** round-trip cost. Negative everywhere in this
sample — exits cannot invent edge; they change how much of a bad cohort we keep.

### All fills (n=414)

| Variant | $ | mean R | win | saved n/$ | cut n/$ miss |
|---|---:|---:|---:|---|---|
| **actual (live)** | −435 | −0.096 | 0.17 | 26 / −32 | **155 / 570** |
| stop + leave-OB + EOD | −589 | −0.122 | 0.33 | — | — |
| stop + EOD only | −830 | −0.156 | 0.35 | — | — |
| ratchet arm **0.15** give 0.35R | −345 | −0.028 | 0.43 | 22 / +10 | 97 / 366 |
| ratchet arm **0.25** give 0.35R | −280 | −0.018 | 0.47 | 17 / +38 | 81 / 293 |
| ratchet arm **0.35** give 0.35R | −299 | −0.023 | 0.48 | 13 / +44 | 78 / 279 |
| **live-ish arm 0.06 + peak 0.35%** | **−174** | −0.021 | 0.42 | 35 / +37 | **150 / 561** |
| arm 0.15 + peak 0.35% | −291 | −0.035 | 0.64 | 21 / +60 | 132 / 491 |
| arm 0.06 + peak 0.50% | −205 | −0.026 | 0.39 | 35 / +35 | 152 / 569 |
| BE@0.35R then leave-OB | −496 | −0.089 | 0.30 | — | — |
| live-like + leave-OB | −245 | −0.028 | 0.35 | 34 / +40 | 128 / 459 |

### Train (≤9/17) vs test (≥9/18)

| Variant | Train $ / meanR / cut | Test $ / meanR / cut |
|---|---|---|
| actual | −129 / −0.10 / 89 | −305 / −0.10 / 66 |
| arm 0.25 give 0.35R | −116 / −0.08 / 40 | −164 / **+0.06** / 41 |
| arm 0.06 + peak 0.35% | **−51** / −0.06 / 77 | −124 / +0.03 / 73 |
| stop+LOB+EOD | −129 / −0.10 | −461 / −0.15 |

**Read:** Current live-ish settings (**arm 0.06 + peak 0.35%**) look best on
total $ in-sample, but they are the **most aggressive cutter** (cut ≈ 4× saved
by count; miss $ ≫ saved $). Raising **arm_r to 0.25** cuts less, wins more
often on test (+0.06 mean R), and matches “trail = backup leash.”

Capture-of-MFE averages are negative/noisy here because most trades never
build meaningful MFE before the shelf — selection dominates (see runway study).

---

## Plain answer

**Is the ratchet protecting profit or cutting runners?**  
**Mostly cutting runners.** On 414 trades: live trail exits **cut short ~155**
times (≈$570 of +0.35R-above-exit opportunity before the initial stop) vs
**saved ~26** (≈−$32, i.e. even “saves” weren’t clean wins in $). The
arm-0.06+peak-0.35 sim: **150 cut / 35 saved**.

**Which rule fits the prefs?**

| Pref | Implication |
|---|---|
| Trail = backup leash | **Raise arm_r** (0.25, or at least 0.15). Stop owning 0–0.25R. |
| left_overbought = thesis | Keep on; do not replace with tighter trail |
| EOD ~15:50 | Keep |
| No no_progress | Keep **off** |

**Recommended simplest set**
1. Initial structure stop.  
2. **left_overbought** (thesis).  
3. Trail as backup: **`arm_r = 0.25`**, give 0.35R **or** peak_give 0.35% (not
   both fighting to be tightest from tick one) — pick **one** cushion model.  
4. EOD 15:50.  
5. stale_data only on a shared freshness clock.  
6. no_progress **off**.

**Drop / avoid:** arm_r 0.06 as default (too early); stacking peak_give on top
of a tight arm without measuring; re-enabling no_progress.

Expected effect if arm moves 0.06 → 0.25 (inferred): fewer `local_trail` scratches
on 0.05–0.15R pops; more room for leave-OB / true runners; may give back a bit
of the “best $” in the arm-0.06 sim — acceptable if the goal is capture, not
scratch-minimization.

---

## Measured vs inferred

| Claim | Status |
|---|---|
| Mechanism, knobs, ship dates | **Measured** (code + git) |
| Variant $/R/win/saved/cut tables | **Measured** on cached bars (1m approx) |
| “Ratchet cuts more than it saves” | **Measured** (cut_n ≫ saved_n) |
| Prefer arm_r 0.25 as leash | **Inferred** from prefs + cut/save ratio |
| Exact live P/L under tick shelf | **Not measured** (1m resolution) |

*Offline until 16:05; Sep-24 bars can refresh after close. Branch `runway-study`.*
