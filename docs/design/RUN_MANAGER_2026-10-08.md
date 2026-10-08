# Run manager: a two-phase exit (design, 2026-10-08, revision 2)

Operator, 10/8: *"The ratchet stop works OK in some situations, but in most it prevents the price from going further … have the ratchet as one of the things that prevent loss and capture profit … when a name is on a profit run, let it continue … look holistically at the position to see if selling is the right move, not just the stop price."*

Status: **design only.** It is built switched off (`ai_exit_run_manager: false`), replayed nightly as `run_manager`, and switched on live only if its pre-registered test passes (`docs/studies/run_manager_prereg.json`).

Revision 2 applies all 16 findings of the 10/8 skeptic review ([Rn]); revision 3 the round-2 items ([R3], [R5], [R10], [R11], [N1]-[N3]).

## Why

Today's exits are separate rules, each selling on its own trigger: the ratchet (R-based trail), the decay leash (+0.05R every 8 s without a new high), the triangle, dead-trade, and the 3-minute no-green cut. None looks at the rest of the position. On a calm $100 name the leash moves the stop about 0.25% of price every 8 s of sideways drift, so a run is sold on its first pause.

This design targets **winners only**. Phase 1 is unchanged; on 10/8, 16 of 32 trades never rose more than 5 bp, and those are an entry problem.

## Inputs, and the rule each obeys (identical live and in the replay)

| Input | Source | Rule |
|---|---|---|
| **Price, peak** | polled `last_seen_price` only [R2] | The +0.15% arm and the S5 peak use polled prices only, never bar highs. |
| **Completed 1-min bars** | live: the desk's IEX 1-min cache; replay: its IEX bar store | A bar is usable only once `bar_start + 60 s + 30 s <= now` (`ai_exit_rm_bar_lag_sec` 30). The same lag applies live and in the replay, so the replay cannot see a bar sooner than live [R1]. **Real bars only**: no gap-filled bars, because a filled bar's low equals the last close and would tighten the stop artificially [N1]. The bar timestamp used is logged on every decision. |
| **Engine fast %R** | live: `_engine_indicators()` (dashboard `signal_proximity`); replay: the recorded `signal_proximity` | A **read** is a distinct engine update. Its key is (engine newest-bar time = observation time − the row's `bars_age_sec`, rounded to the second; the `pctr` value). A read is new when either part changes; the same key twice is one read [R5]. The same key is used live and in the replay (the replay ages `bars_age_sec` with its clock, `AGE_KEYS`). A row with `bars_age_sec` > 90 s is no signal, so the position holds on that signal. |
| **Resistance level** | `ob_observe._charted_at(sym, now)`, recomputed at each usable bar for held names (not the cache-only `levels()`) [R3] | Nearest charted resistance block with **bottom strictly above the price** at that moment. A block that contains the price is not a level [R4]. **Age = now − (start of the newest bar in the order-block store + 60 s)** (equivalently `ob_observe.absorb_age`), so a level built on a store that stopped updating is old even though `_charted_at` keys on the current minute. Above **240 s** (`ai_exit_rm_level_max_age_sec`; the warm worker refreshes each name at most every 120 s) there is no level, so S3 is inactive. The replay copies the warm worker's 120 s refresh cadence. Held names must stay in the warm-fetch set after they leave the book (the build proves this with a test). The level age is logged. |

## Phases

### Phase 1: protect (from the fill until +0.15%)

**Unchanged from live (np180):** initial shelf and R-ratchet, decay leash, the 3-minute no-green cut, dead-trade, 15:50 flatten. The triangle and the global %R dump stay off, as in np180.

**Phase 2 starts** the first poll where the polled peak is ≥ **+0.15%** above the entry fill (`ai_exit_rm_arm_pct`).

### Phase 2: let it run

**Exits switched off in phase 2** [R8]:
- the R-trail (`local_profit_stop`)
- the decay leash (`green_catchup_raise`)
- dead_trade
- `no_progress` (already off by definition once green)
- the legacy left_overbought exit

**Kept:** the 15:50 flatten, and the T1 scale-out exactly as live (1R target, rarely reached).

**Net stop** (written into `local_stop_price`, only ever raised) [R7] = max of:
- the **inherited `local_stop_price`** at the moment phase 2 starts (it never lowers the phase-1 stop);
- entry + `ai_breakeven_offset_px`;
- the **higher-low stop**: the lowest low of the last 3 usable completed 1-min bars (`ai_exit_rm_hl_bars`), minus $0.01, recomputed at each newly usable bar.

**Sell on the first of these** (checked every poll; the reason is logged):

| # | Signal | Exact rule |
|---|---|---|
| S1 | **Triangle** | The engine fast %R was ≥ −20 on a read in phase 2, and is < −20 on **2 consecutive distinct reads**. |
| S2 | **%R dump** | `ai_positions.rsi_dump_due` with a **private cfg** (enabled, 30 points, 60 s, confirm 2), **called only on a new distinct engine read** (never on every 2 s exit tick, because it counts its streak per call), so 'confirm 2' means 2 distinct engine reads [N3]. The global `ai_exit_rsi_dump_enabled` stays **off**, so phase 1 is untouched [R6]. |
| S3 | **Into resistance** | Price ≥ the current resistance level's bottom × (1 − 0.02%) (`ai_exit_rm_res_pad_pct`). |
| S4 | **Structure break** | Price ≤ the net stop. |
| S5 | **Give-back cap**, wide runs only | **Arms** once the peak is ≥ max(+0.40%, 2 × the entry spread) above the entry, where the entry spread is the recorded SIP NBBO spread at the entry (as in the 8/21 guard, which delays arming until the move clears k spreads) [R10]. Once armed: sell when price ≤ peak − 50% × (peak − entry). Before it arms there is no give-back rule, so phase 2 is never tighter than a plain breakeven net. |
| S6 | **Clock** | 15:50 flatten (existing). |

Holding is the absence of a sell signal: %R still in the band, higher lows intact, below the next resistance, and (on wide runs) less than half the profit given back.

## Knobs (all new, default off or neutral)

`ai_exit_run_manager` (false), `ai_exit_rm_signals` (`all` | `s5_only`: S1-S3 off, for the mechanism null) [N2], `ai_exit_rm_arm_pct` 0.15, `ai_exit_rm_bar_lag_sec` 30, `ai_exit_rm_hl_bars` 3, `ai_exit_rm_res_pad_pct` 0.02, `ai_exit_rm_level_max_age_sec` 240, `ai_exit_rm_gb_min_pct` 0.40, `ai_exit_rm_giveback` 0.50, `ai_exit_rm_signal_max_age_sec` 90.

## What it does not do

- It does not change entries, phase 1, sizing or the 15:50 clock.
- It does not use the live clock-window %R recompute.
- It does not trade live until the prereg passes.

## Build order

1. Prereg plus skeptic review (round 2).
2. `ai_positions.run_manager_exit(pos, engine_reads, bars, level, cfg, now)`: a pure function returning (sell, reason, net_stop, diagnostics), with a test for every rule above, including the look-ahead guards (bar lag, distinct reads, inside-block level).
3. The live exit loop calls it only when the knob is on, and logs the inputs it used.
4. `tools/replay_session.py` calls the same function on recorded inputs, with the same bar lag, and `_charted_at` for held names.
5. The nightly replay variant `run_manager` (switches pinned: `$NP_ON $LOB_OFF`, dump off) [R9] and the information variant `s5_only` (the plain give-back trail of S5, without S1-S4) as the mechanism null [R10].
