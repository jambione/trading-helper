# Entry pricing policy + buy_zone auto-limit (monitor)

**Status:** approved 2026-08-04 · **implemented** (monitor path, default OFF)  
**Scope:** Adaptive limit price policy + optional auto-fire on `buy_zone` rising edge + risk bracket + ratchet + daily −2R halt.

## Goal

When a momentum name enters engine **`buy_zone`**, the monitor may place an **Alpaca limit** sized by desk `$` budget, with **limit price from an adaptive policy** (passive → join ask). Maximize fill quality / entry edge vs blind ask+pad, with hard safety gates.

## Decisions (approved)

| Choice | Value |
|--------|--------|
| Hot signal (v1) | **`buy_zone` only** (rising edge) |
| Pricing | **Adaptive passive → join** |
| Auto mode | **Paper-only** until `auto_limit_live=true` |
| Default | **`auto_limit_enabled=false`** |
| Risk model | **Tight stop + small share size** (fixed % of equity at risk) |
| Exits | Bracket: TP top + SL bottom (OCO); SL **ratchets up** as trade goes green |
| Compounding | Capture profit → flat → recycle full BP; **session halt at −2R** so a bad morning can’t reverse the day |

## Risk model (approved 2026-08-04 follow-up)

**Philosophy:** Prefer small, controlled losses over large size with a wide stop.
Share count is derived from risk, not a fixed notional alone.

```text
risk_dollars = equity × risk_pct          # e.g. 0.35% of equity
stop_dist    = entry − stop_px            # tight, e.g. 0.40% of entry
shares       = floor(risk_dollars / stop_dist)
# also cap by max_notional if set: shares ≤ floor(max_notional / entry)
```

### Initial bracket (at fill)

| Leg | Rule (v1 defaults — tunable) |
|-----|------------------------------|
| **Entry** | Policy limit (or join if urgency high / open) |
| **Bottom (stop)** | `entry × (1 − stop_pct)` with `stop_pct ≈ 0.40%` |
| **Top (target)** | `entry + reward_R × (entry − stop)` with `reward_R ≈ 2.0` |
| **OCO** | TP and stop linked; one fill cancels the other |

### Variable stop (raise only — never lower)

| Milestone | Stop action |
|-----------|-------------|
| Fill | Tight initial stop under entry |
| Unrealized ≥ **+1R** | Move stop to **breakeven** (entry ± tick) |
| Unrealized ≥ **+2R** | Move stop to **+1R** (lock a win) |
| Optional after +2R | Swap to **broker trailing stop** (e.g. 0.6–1.0% daytrade trail) |

R = initial risk per share = `entry − initial_stop`.

### Why tight + small size

- Caps loss per trade to ~`risk_pct` of equity even if stopped immediately.
- Accepts more stop-outs at the open; avoids “wide stop, large size, one bad trade kills the day.”
- After flat, full buying power recycles into the next `buy_zone`.

### Session risk cap (approved — protect compounding)

**Goal:** bank and recycle purchasing power; a bad open must not erase the day.

After each closed trade, track **session P&amp;L in R-units** (relative to that trade’s planned risk dollars at entry):

```text
trade_R   = realized_pnl / risk_dollars_at_entry
session_R = sum(trade_R) over the ET session
```

| Rule | Suggested default |
|------|-------------------|
| **Daily stop** | If `session_R ≤ −daily_loss_r` → **disable auto-limit for the rest of the ET day** |
| **`daily_loss_r`** | **`2.0`** (stop auto after about −2R on the day) |
| Manual desk | Unaffected (or same flag optional later) |
| Reset | New ET calendar day (same as `auto_limit` session counter) |
| Live flag | Cap applies whenever auto is on (paper and live) |

Optional later (not v1): **daily win target** (e.g. +4R → pause auto and protect gains).

Example: three scratches at −1R each → session_R = −3 ≤ −2 → auto off; human can still trade; tomorrow resets.

### Config knobs (planned)

| Key | Suggested default |
|-----|-------------------|
| `risk_pct` | `0.35` (% of equity) |
| `stop_pct` | `0.40` (% below entry) |
| `reward_r` | `2.0` |
| `be_at_r` | `1.0` (move to BE at +1R) |
| `lock_at_r` | `2.0` (move stop to +1R at +2R) |
| `trail_pct` | `0.8` (optional after lock) |
| `max_notional` | optional hard $ cap |
| **`daily_loss_r`** | **`2.0`** (session auto-halt when cumulative R ≤ −this) |
| `daily_loss_halt_auto` | `true` |

## Non-goals (v1)

- TradingView order ticket UI automation  
- LOOK / burst auto-fire (config list reserved for later)  
- Unfilled-order chase / reprice loop  
- Full multi-scale pyramid (single OCO full exit first; scale later)

## Architecture

```text
/api/state  →  monitor Feed
                 │
                 ├─ buy_zone rising edge?
                 ├─ gates (enabled, paper|live flag, cooldown, session cap, flat)
                 ├─ entry_pricing.decide(bid, ask, rvol, proximity, …)
                 └─ alpaca buy_limit_at_price(limit_px, $)
```

### `entry_pricing.py` (pure)

**Inputs:** bid, ask, last?, rvol?, proximity_pct?, session_open?, pad_max_pct, max_spread_pct  

**Output:** `PricingDecision(ok, style, limit_px, urgency, reason)`  
Styles: `passive` | `fair` | `join` | reject  

**Urgency (0–1):** higher RVOL + higher proximity + tighter spread → more urgent.  

**Price:**
- passive → near bid (or bid+tick)  
- fair → mid  
- join → ask × (1 + pad) capped by pad_max  
- Clamp: bid ≤ limit ≤ ask×(1+pad_max); reject bad quotes / wide spread  

### `desk_actions.desk_buy_policy`

Fetches IEX bid/ask, calls `decide`, submits via `buy_limit_at_price`.  
Manual `buy_order_style=policy` uses same path.

### Monitor auto arm

Config keys (DEFAULTS + optional JSON):

| Key | Default |
|-----|---------|
| `auto_limit_enabled` | false |
| `auto_limit_live` | false |
| `auto_limit_signals` | `["buy_zone"]` |
| `auto_limit_cooldown_sec` | 900 |
| `auto_limit_max_per_session` | 3 |
| `entry_pad_max_pct` | 0.15 |
| `entry_max_spread_pct` | 1.0 |
| `buy_order_style` | auto \| limit_ask \| market \| **policy** |

Gates: trader active; paper OR (live AND auto_limit_live); not already long; no duplicate open buy; cooldown; session cap; decision.ok.

### Constructive tape gate (approved — avoid downtrends)

Uses engine `signal_proximity` (CM RSI / %R exhaustion / MACD legs already on the row):

| Check | Default |
|-------|---------|
| `auto_limit_require_constructive` | **true** |
| Block if `sell_signal` | yes |
| `proximity_pct` ≥ `auto_limit_min_proximity_pct` | **67** (yellow+) |
| Block if both `pctr_falling` and `pctr_slow_falling` | yes |
| Require `buy_signal` | **false** (optional stricter) |
| Missing proximity / no bars | skip (no inventing) |

```text
buy_zone rising edge
  AND not sell_signal
  AND proximity ≥ 67
  AND not (pctr_falling AND pctr_slow_falling)
  → then risk-sized limit + bracket
```

On success: status line + journal `auto_limit`.

## Testing

Pure pricing unit tests; gate/cooldown tests; no live orders in CI.
