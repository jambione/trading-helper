# Phase B — Premarket session extension (design)

**Status:** scoreable paper lane (2026-09-17 pack #6). Hybrid C. Not Plan B burst.  
**Live bot_config (mini):** `ai_phase_b_enabled=true`, `ai_phase_b_dry_run=false` — real Alpaca paper extended DAY limits.  
**Defaults (fresh checkout):** `ai_phase_b_enabled=false`, `ai_phase_b_dry_run=true` until human enable.  
**Modules:** `phase_b.py`, `phase_b_ledger.py`, `tools/phase_b_scoreboard.py`.  
**Paper fill caveat:** `docs/PHASE_B_ALPACA_PAPER_EXTENDED_FILLS.md` — do not trust first PASS as live edge.

## Why

RTH is a fixed ~6.5h ceiling. Phase B adds ~04:00–09:30 ET of *tradable* time so opportunity count (armable minutes × opens) can rise without squeezing Plan A knobs. Profit thesis: extra session → more shots at MFE → trail/working-sell capture — only if fills stay honest on thin tape.

## Naming (do not conflate)

| Name | Meaning |
|------|---------|
| Plan A | Live EXH+RSI, RTH only |
| Plan B burst | Premarket *universe* scout → entry still at/after 09:30 RSI≥70 (gated off) |
| **Phase B** | Actually trade ~04:00–09:30 (this doc) |

## Strategy: Hybrid C (locked)

- **Universe:** momentum seed **primary**, then movers, mention burst, trending
- **Arm:** stripped EXH+RSI on Finnhub **stream-last**
- **Orders:** Alpaca extended = DAY **limit** + `extended_hours=True` only
- **Data:** free path — no SIP required for v1; IEX book treated as blind
- **Isolation:** own seats, slots, ledger, P&L tag `phase_b`; never OR into Plan A arms

## Session / order mechanics (locked)

| Clock (ET) | Rule |
|------------|------|
| 04:00–09:20 | Arms + new entry limits allowed |
| 09:20 | No new entries; cancel resting buys |
| 09:25 | Flatten window starts |
| 09:28 | Must be flat |
| 09:30+ | SOD wipe = failure backup only |

- **v1: no handoff into Plan A** (no `handoff_working_sell_to_rth` for Phase B lots)
- **Entry:** limit on stream-last + pad (start ~0.15% / ~$0.05 cap); **TTL 45s** then cancel → `phase_b_unfilled`
- **Exit:** working limit sell (Phase B only); chase **2.5s**; max slip **0.25R** (flatten may one-step wider); last-print age **≤15s**; hard stop **−5%** on last (software only)
- Existing knobs to reuse/scope: `ai_entry_limit_*`, `ai_premarket_working_sell`, `ai_premarket_chase_step_sec`, `ai_premarket_max_exit_slip_r`, `ai_premarket_quote_max_age_sec`

## Arm strip (locked)

- Price = Finnhub last only; refuse if print age **>15s**
- EXH **40–70** and **rising** (tighten only after score)
- RSI: **block only** when falling and RSI **>10**
- **Off:** `cm_rsi_max`, mistimed_heat, soft_ob, MACD require/gap/narrow
- Confirm: **1** fresh last tick
- No IEX ask/bid in arm decision

## Universe / book (locked, revised)

**Sources (priority):**
1. **Momentum seed — primary** (most valuable premarket)
2. Movers
3. Mention burst
4. Trending
5. **Off v1:** research / seed_rank

**Size:** 4–6 Phase B seats; **max 1–2 concurrent opens**; isolated from RTH elite pins.

**Price:** **do not skip sub-$5** — floor = desk `ai_watch_min_price` (~$2). Thin-tape risk managed by print-age, TTL, and small size — not by cutting momentum names.

**Drop:** no fresh last ~60–90s after seat → demote; drain-only after 09:25.

## Build-around limitations

1. No market orders / no brackets outside RTH
2. No broker stop outside RTH (`ai_broker_stop_enabled` stays false; software protection)
3. Free data: Finnhub trades/last for arms & stops; do not trust IEX ask/spread
4. `place_scaled_entry` / `trading_hours_active` today refuse overnight buys — Phase B needs an explicit session flag, not a silent RTH bypass
5. SIP deferred until desk earns *or* score proves book fiction is the choke

## Scoreboard + pass/fail (locked)

Score **only** rows tagged `phase_b`. Never mix into RTH Phase 1/2 gates. Pre-register bars before the paper window; do not retune on the same days you score.

### Metrics (always log)

| Metric | Meaning |
|--------|---------|
| `n_armed` | Arm fires (stream-last + EXH/RSI strip) |
| `n_entry_limits` | Limits submitted |
| `n_fills` / `fill_rate` | Fills / limits |
| `unfilled_pct` | TTL cancels + dead cancels |
| `entry_slip_vs_last` | Fill px vs arm last (bps or %) |
| `exit_slip_vs_last` | Exit fill vs signal last |
| `mfe_r` / `mfe_pct` | Max favorable excursion after fill |
| `realized_r` / `capture` | Realized vs MFE when MFE is above noise |
| `flat_on_time_pct` | Flat by 09:28 (SOD wipe = fail for that lot) |
| `peak_opens` | Concurrent Phase B opens |
| `day_pl` / `expectancy` | After stated friction |

### Fill model

- Arm on fresh last; earliest honest fill is the **limit fill**, not the signal print.
- Pass/fail uses **actual paper fills** when the dry lane is on; counterfactual replay uses limit-at-last+pad with next print within TTL (miss = unfilled). Do **not** score as market-at-signal.

### Stated friction (round-trip)

**0.50%** round-trip for Phase B paper pass/fail (wider than RTH — thin tape + limit chase). Changing friction after seeing results voids the pass.

### Pass (all required)

Window: at least 5 Phase B sessions **or** `n_fills >= 25` (need enough days either way).

1. **`n_fills >= 25`** (or >=5 days with >=3 fills each) — else NO DECISION
2. **`fill_rate >= 40%`** of entry limits — else FAIL (TTL/pad/universe broken)
3. **Expectancy after 0.50% friction > 0** *or* sum PL% > 0 on the window — else FAIL
4. **Median MFE >= 0.10R** (or >=0.3% if R undefined) — else FAIL (volume without MFE)
5. **`flat_on_time_pct >= 95%`** — else FAIL (session contract broken)
6. **Median entry slip vs last <= +0.40%** — else FAIL (pad/chase fiction)

### Fail-fast (any one)

- Only win% looks good while expectancy / med MFE <= 0
- SOD wiped any Phase B lot (counts against flat_on_time)
- Peak opens routinely >2 (cap not held)
- Retuning arm/TTL on the scoring window

### Informational (not pass metrics)

Win%, momentum vs movers vs burst mix, unfilled reason mix, time-of-day heatmaps.

### Live enable bar

Paper PASS on the pre-registered window **and** a second confirm week without knob changes. Size stays 1-2 opens until that confirm. SIP still not required to pass.


## Practice path (before live)

1. Paper dry lane with session flag + limit path + working sell + flat-by-09:28
2. Run Phase B scoreboard on the metrics above (separate file/tag from RTH)
3. Pass/fail = **Scoreboard + pass/fail** section only — not RTH Phase 1/2
4. Size-up only after PASS + confirm week

## Implement slices

1. ~~`phase_b_session` flag + clock gates + flatten-before-open~~ shipped (dry default)
2. ~~Price path: arm/stop/limit off Finnhub last~~ shipped
3. ~~Limit entry + working-sell exit on that flag~~ shipped (dry = no broker)
4. ~~Universe wiring (momentum-first) + 4–6 seat book + 1–2 open cap~~ shipped
5. ~~Phase B ledger / scoreboard~~ shipped (`tools/phase_b_scoreboard.py`)

## Non-goals v1

- Not enabling Plan B burst live
- Not SIP
- Not green catch-up complexity until Phase B MFE exists
- Not sharing RTH elite pin seats
