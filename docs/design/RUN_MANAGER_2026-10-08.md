# Run manager: a two-phase exit (design, 2026-10-08)

Operator, 10/8: *"The ratchet stop works OK in some situations, but in most it prevents the price from going further … have the ratchet as one of the things that prevent loss and capture profit … when a name is on a profit run, let it continue … look holistically at the position to see if selling is the right move, not just the stop price."*

Status: **design only.** It is built switched off (`ai_exit_run_manager: false`), replayed nightly as `run_manager`, and switched on live only if its pre-registered test passes (`docs/studies/run_manager_prereg.json`).

## Why

Today's exits are separate rules, each selling on its own trigger: the ratchet (R-based trail), the decay leash (+0.05R every 8 s without a new high), the triangle, dead-trade, and the 3-minute no-green cut. None of them looks at the rest of the position. On a calm $100 name the leash moves the stop about 0.25% of price every 8 s of sideways drift, so a healthy run is sold on its first pause (TJX 10/7: 53 s; the operator's "ratchet chase").

10/8 to 10:53: 32 trades, −6.9 bp. 16 of 32 never rose more than 5 bp after entry, so most losses are entries. Winners peaked at a median +17 bp and kept +7.9 bp. **This design targets the winners only: phase 1 is unchanged.**

## Phases

### Phase 1: protect (from the fill until the run is confirmed)

**Unchanged from live**: initial shelf and ratchet, decay leash, the 3-minute no-green cut (np180), dead-trade, 15:50 flatten. Keeping phase 1 identical means any difference in the test comes from phase 2 alone.

**Phase 2 starts** the first time the position's best price reaches **+0.15% above the entry fill** (`ai_exit_rm_arm_pct`). That is about 1.5–3× a tight name's spread, and about half the live ratchet's R-based arm (+0.30% on a 5% stop).

### Phase 2: let it run

On entering phase 2, the R-trail and the decay leash **stop moving the stop**. The stop becomes a **safety net**:

- **Net stop** = max(entry + `ai_breakeven_offset_px`, **higher-low stop**). A run that turns red is never held.
- **Higher-low stop** = the lowest low of the last **3 completed 1-minute bars** (`ai_exit_rm_hl_bars`), minus 1 cent. It is recomputed when each 1-minute bar completes and **only ever raised**.

**Sell when any of these happens**, checked every poll with the reason logged:

| # | Signal | Exact rule |
|---|---|---|
| S1 | **Triangle** | Fast %R from the **engine's minute-grid line** (smoothed %R(21) EMA(7), the chart-matching line) was ≥ −20 while in phase 2 and is now < −20 on 2 consecutive reads. Never uses the live clock-window recompute (the 10/8 raw-fallback bug). |
| S2 | **%R dump** (red knife) | `ai_positions.rsi_dump_due` on the same engine line: peak in the last 60 s ≥ −20, now ≥ 30 points below, 2 agreeing reads. |
| S3 | **Into resistance** | Price ≥ the bottom of the nearest charted resistance block above (`ob_observe.levels` → `ob_res_btm`, point-in-time, last 3 per side) minus 0.02% (`ai_exit_rm_res_pad_pct`). The level is read when phase 2 starts and refreshed each completed bar. No level known means S3 is inactive. |
| S4 | **Structure break** | Price ≤ the net stop (above). |
| S5 | **Give-back cap** | Price falls by ≥ 50% of the open profit (peak − entry) from the peak (`ai_exit_rm_giveback`), once the peak is ≥ +0.15%. This is the one ratchet-like rule kept, so a reversal that S1, S2 and S4 have not yet confirmed still keeps half the run. |
| S6 | **Clock** | 15:50 flatten (existing). |

Holding is simply the absence of a sell signal: %R still in the band, higher lows intact, below the next resistance, and less than half the profit given back.

## Data each signal needs (live and replay must match)

| Input | Live | Replay |
|---|---|---|
| Engine fast %R (S1, S2) | `_engine_indicators()` (dashboard `signal_proximity`), with its timestamp; stale > 90 s = no signal (hold) | recorded `signal_proximity` |
| Completed 1-min bars (net stop) | the desk's IEX 1-min cache (`symbol_ohlc` and stamps), gap-filled like `signals._minute_grid_pr` | the replay's bar store (IEX), same fill rule |
| Resistance level (S3) | `ob_observe.levels(sym, price)` | the same, from the replay's order-block store |
| Price | `last_seen_price` | recorded tape |

The build must prove each input is present in the replay. Counts of phase-2 entries and of each sell reason are reported every night. **A variant that never enters phase 2, or never fires S1 or S3, is vacuous and is labelled so before any read.**

## Knobs (all new, default off or neutral)

`ai_exit_run_manager` (false), `ai_exit_rm_arm_pct` 0.15, `ai_exit_rm_hl_bars` 3, `ai_exit_rm_res_pad_pct` 0.02, `ai_exit_rm_giveback` 0.50, `ai_exit_rm_signal_max_age_sec` 90.

## What it does not do

- It does not change entries, phase 1, sizing or the 15:50 clock.
- It does not use the live %R recompute; the triangle bug fix stays a separate change.
- It does not trade on any of this live until the prereg passes.

## Build order

1. Prereg plus skeptic review (this document and `run_manager_prereg.json`).
2. `ai_positions`: `run_manager_exit(pos, sig, bars, levels, cfg, now)`, a pure function returning (sell?, reason, new_net_stop), with tests for every row of the signal table.
3. The live exit loop calls it only when the knob is on.
4. `tools/replay_session.py` `_live_exit` calls the same function on recorded inputs, with a test.
5. Nightly replay variant `run_manager` against `np180`; review section showing phase-2 counts, sell reasons, capture ratio and give-back.
