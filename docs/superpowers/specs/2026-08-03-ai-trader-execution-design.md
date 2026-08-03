# AI Trader Execution: Agreement Book + Realtime Arming

**Date:** 2026-08-03  
**Status:** Draft for review  
**Scope:** When and how the AI paper desk opens and closes trades — not research prompt content, not live (non-paper) trading.

## Problem

Today, **new buys only run** after scheduled research (e.g. 08:25 / 11:00 / 13:00 ET) or a once-per-day open-bell (~09:35). Between those moments the process manages open positions but does **not** re-try entries.

When the entry CLI returns `WAIT`, levels are forced to zero, the event is logged as `entry_not_qualified`, and the name is not watched. Missing a window (or getting WAIT) feels like “losing the day,” even when both AIs still agree on the idea.

**Sells/exits** for open positions are already continuous (mechanical stops, targets, trail, time-stop; thesis-break on research). That path is largely correct; this design focuses on **entries**, with light sell clarifications.

## Goals

1. **Agreement ⇒ trade intent.** If both AIs list a symbol, it belongs on an **active execution queue** for the session (buy when ready), not only on a dashboard suggestion list.
2. **WAIT ⇒ timing, not rejection.** WAIT arms a **per-symbol poll** during RTH until conditions hit, invalidation, or policy expiry — not “try again at the next research slot only.”
3. **Market data decides when.** Alpaca quotes (and existing book quote/volume refresh) drive arming; the LLM decides structure and thesis, not the wall clock alone.
4. **Research remains the slow clock.** Research refreshes the book and thesis-break reviews; it is **not** the only buy button.
5. **Quality and risk gates stay.** Caps (max positions, risk %, spread, min R:R, buy rate limits) still apply **per attempt**, and must not silently ban a symbol for the rest of the day without an explicit invalidation.

## Non-goals

- Live (non-paper) trading or multi-broker routing.
- Short selling dual “avoid” names (flat + avoid ⇒ do not buy; hold + thesis break ⇒ exit long).
- Continuous full LLM entry calls every few seconds (too costly / noisy).
- Replacing Finviz/Finnhub for tick triggers (they remain universe / research helpers).

## Current vs target

| Concern | Current | Target |
|--------|---------|--------|
| Dual-AI agreement | Filter on entry pass only | Promotes to **session watch queue** |
| WAIT | End of attempt; levels wiped | **Arm poll** with structured reason + levels when possible |
| Buy timing | Research + open bell only | **RTH poll** on queue using Alpaca |
| Sell timing | Continuous mechanical + research thesis | **Unchanged** (keep continuous) |
| After last research slot | No more new entries | Queue still polled until EOD / invalidation |
| Observability | Often only `decision: WAIT` | Persist full entry JSON + poll transitions |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ SLOW CLOCK — Research (existing times, weekdays)               │
│ Grok + Claude → ranked rows → agreement tag                    │
│ Thesis-break review for open positions                         │
└────────────────────────────┬─────────────────────────────────┘
                             │ upsert / supersede
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ AGREEMENT / HOT BOOK                                           │
│ Prefer symbols with agreement=true (both sources)              │
│ Optional: top single-source names at lower priority (config)   │
└────────────────────────────┬─────────────────────────────────┘
                             │ ensure structure
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ ENTRY STRUCTURE (rare LLM)                                     │
│ BUY with levels  |  WAIT with wait_kind + levels/summary       │
│ Persist decision JSON; TTL / re-structure rules                │
└────────────────────────────┬─────────────────────────────────┘
                             │ while RTH & not filled
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ FAST CLOCK — Execution poll (new; ai_trader loop)               │
│ Alpaca: open? bid/ask/spread/last; held? open orders?          │
│ Gates → if armed, place_scaled_entry (paper brackets)          │
└────────────────────────────┬─────────────────────────────────┘
                             │ if filled
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ POSITION MANAGEMENT (existing)                                 │
│ manage_open_positions ~5s: stops, scale-out, trail, time-stop  │
│ Thesis exit only on research position_reviews                  │
└──────────────────────────────────────────────────────────────┘
```

### Components

1. **Watch queue state** (new JSON, e.g. `claude_reports/entry_watch_state.json` or under existing AI state dir)
   - Per symbol: source marks, scores, research reason, structure decision, wait_kind, levels, armed_at, last_poll, last_quote, status (`watching` | `armed` | `submitted` | `filled` | `invalidated` | `expired`).
2. **Queue builder** — after research merge / open bell book refresh: upsert agreed names; drop names no longer on either book (policy below).
3. **Structure resolver** — call entry CLI when queue member lacks a valid structure or structure TTL expired; parse and store full decision.
4. **Poller** — on `ai_trader` loop during RTH (interval config, e.g. 15–30s): for each `watching`/`armed` symbol, fetch Alpaca quote, evaluate arm rules, place order if ready.
5. **Event log** — extend `events.jsonl`: `watch_add`, `watch_arm`, `watch_skip`, `entry_ok`, `structure_wait`, `structure_buy`, `invalidated`, etc., always with summary/levels when present.

## Execution policy

### Who enters the queue

| Condition | Default |
|-----------|---------|
| `agreement=true` (both AIs list symbol) | **Always** enqueue for buy-when-ready (if trading source enabled and market session policy allows) |
| Single-source only | **Optional** (`ai_watch_single_source=false` by default) |
| Score &lt; min_score | Still enqueue if agreement; score used as priority only **or** soft-skip structure until score recovers (default: **enqueue if agreement**, log low_score as priority note) |
| Already held / max positions | Stay on queue but poll no-ops with `already_held` / `max_positions` until capacity frees |
| Above max price | Do not arm; invalidate or hold as `blocked_price` until research removes or price ok |

### WAIT semantics (change from today)

Prompt + parser change: WAIT must **not** zero useful structure when the thesis is intact.

| `wait_kind` | Meaning | Poll behavior |
|-------------|---------|----------------|
| `wait_for_zone` | Want buy only in `[entry_low, entry_high]` with stop/target set | **Auto-buy** when ask in zone (pad optional), spread OK, R:R still valid |
| `wait_setup` | No clean levels yet | Poll for **significant** price/volume change; then **re-run structure** (rate-limited), not buy blind |
| `hard_no` | Do not trade this idea until next research | Remove from active poll; status `invalidated` |

If the model omits `wait_kind`, infer: levels present ⇒ `wait_for_zone`; else `wait_setup`.

**BUY** with full levels: either place immediately if ask in zone and gates pass, or enqueue as `armed` with same zone rules (avoid chasing if price already blown through zone — config `ai_chase_pct` or require ask ≤ entry_high).

### Arm / buy conditions (data)

All must pass for a new paper buy:

1. `market_is_open()` (Alpaca).
2. Trading owner ready (Grok paper path as today).
3. Not already held; under `ai_max_positions`; under buy caps (`ai_max_buys_per_poll` / daily if added).
4. Spread ≤ `ai_max_spread_pct`.
5. Ask ≤ `ai_max_price` (and book max_price).
6. For `wait_for_zone` / BUY structure: ask within `[entry_low, entry_high]` (optional pad).
7. `reward_risk` ≥ `ai_min_reward_risk` on stored structure.
8. Pre-entry portfolio gates (open risk %, daily loss R) unchanged.

On success: existing `place_scaled_entry` + `record_external_buy`; status `submitted`/`filled`.

### When queue members die

| Event | Action |
|-------|--------|
| New research: symbol dropped from **both** books | Invalidate watch (thesis withdrawn) |
| New research: still agreed | Refresh reason/scores; keep structure if still valid else re-structure |
| `hard_no` | Invalidate until next research |
| Structure TTL (e.g. 90 min) without fill | Re-structure once; if still WAIT without zone, keep `wait_setup` with backoff |
| Session end (RTH close) | Expire watch for day **or** carry overnight (default: **expire unfilled watches at RTH close**; next day needs research/open-bell rebuild) |
| Fill | Remove from watch; position manager owns it |

### Open bell

Keep as a **kickstart**: ensure queue built from current book and structure resolved early after open. It is no longer the only post-open entry opportunity.

### Research times

Unchanged as thesis refresh. After each research that runs with trading on, rebuild queue and run structure for new/changed agreed names; poller continues independently.

## Sells (explicit)

| Path | Behavior |
|------|----------|
| Hard stop / target / scale-out / trail / time-stop | **Keep** continuous mechanical via `manage_open_positions` + resting Alpaca brackets |
| Thesis break | **Keep** on research `position_reviews` → close |
| WAIT on entry | **Never** implies sell |
| Dual-AI “bearish” while flat | **Do not buy**; no short in v1 |

Optional later (out of v1): data-driven trail tighten using bars/ATR without LLM.

## Data sources

| Source | Role |
|--------|------|
| **Alpaca** | Primary poll: quotes, market clock, positions, orders, paper brackets |
| **Existing `refresh_quotes` / RVOL** | Optional arm filters (min RVOL, dollar volume) |
| **Massive / engine bars** | Optional later for VWAP/zone quality — not required for v1 |
| **Finviz / Finnhub** | Universe / research only — not poll triggers |
| **Grok/Claude CLI** | Research + rare entry structure; not every poll tick |

## Config (new / clarified)

Suggested keys (defaults conservative):

| Key | Default | Purpose |
|-----|---------|---------|
| `ai_watch_enabled` | `true` | Master switch for poll execution |
| `ai_watch_require_agreement` | `true` | Only agreed names on queue |
| `ai_watch_single_source` | `false` | Allow non-agreed top ranks |
| `ai_watch_poll_sec` | `20` | Poll interval while RTH |
| `ai_structure_ttl_sec` | `5400` | Re-structure after 90m |
| `ai_watch_expire_at_close` | `true` | Drop unfilled watches at RTH close |
| `ai_entry_zone_pad_pct` | `0.15` | Slight zone pad |
| `ai_max_structure_calls_per_hour` | `12` | Cap LLM structure spend |
| `ai_persist_entry_decisions` | `true` | Always log full decision JSON |

Existing: `ai_require_agreement`, `ai_min_reward_risk`, `ai_max_spread_pct`, `ai_max_positions`, `ai_open_bell_*`, research times — remain.

## Observability

- Dashboard/API: watch queue summary (symbol, wait_kind, zone, last ask, status, age).
- Events: full decision on every structure call (including WAIT summary).
- Token metrics: phase `entry` / new phase `structure` rate-limited and countable.

## Failure modes

| Failure | Behavior |
|---------|----------|
| Alpaca quote fail | Skip symbol this tick; retry next poll |
| Structure CLI fail | Backoff; leave prior structure if any |
| Partial bracket fail | Existing rollback behavior in `place_scaled_entry` |
| Poller exception | Log; do not kill research loop |
| Duplicate orders | Existing cancel/dedupe + one active watch status `submitted` |

## Testing (acceptance)

1. Unit: WAIT with levels → `wait_for_zone`; arm when mock ask enters zone; no arm when spread wide.
2. Unit: agreement upsert/remove on research merge.
3. Unit: `hard_no` not polled; `wait_setup` does not buy without structure.
4. Integration-style: mock market open + queue → one `place_scaled_entry` call.
5. Regression: `manage_open_positions` still runs every poll interval with no watch entries.
6. Manual paper: agreed name WAIT at open → later fill when price enters zone without waiting for next research hour.

## Implementation phases

### Phase 1 — Observability + structured WAIT (low risk)

- Persist full entry decision (summary + numbers) on every entry skip/ok.
- Prompt: allow WAIT with levels + `wait_kind`; stop forcing zeros when zone known.
- No behavior change to schedule yet (optional feature flag).

### Phase 2 — Watch queue + poller

- State file, enqueue agreed names after research/open bell.
- RTH poll loop in `ai_trader` using Alpaca gates + `place_scaled_entry`.
- Config flags; default on for paper when `ai_watch_enabled`.

### Phase 3 — Polish

- Dashboard watch panel; structure call rate limits; EOD expire; metrics.
- Optional single-source watch; optional RVOL arm filter.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Chasing extended names | Zone upper bound; no buy if ask &gt; entry_high (+ pad policy) |
| LLM flip-flop | Structure TTL; don’t re-call every poll |
| Too many structure calls | Hourly cap; only agreed names |
| Overnight gap risk | Default expire watches at close |
| Agreement noise | Keep min R:R and spread; position caps |

## Success criteria

- Unfilled agreed idea after WAIT is still eligible the **same RTH day** without a new research slot.
- At least one paper path: WAIT → zone touch → bracket without waiting for 13:00.
- Sells remain continuous; no regression in stop/target handling.
- Operators can see *why* WAIT and *what* is being polled.

## Open items (resolved for v1)

| Item | Decision |
|------|----------|
| Agreement ⇒ want trade | Yes |
| WAIT ⇒ poll until ready | Yes |
| Data primary | Alpaca |
| Auto-buy on zone with cached levels | Yes for `wait_for_zone` |
| Re-LLM on every zone touch | No if levels valid; yes for `wait_setup` on significant move |
| Shorting | No in v1 |
| Expire unfilled at close | Yes (default) |

## Approval

- [ ] Product/owner approves this design  
- [ ] Then: implementation plan (`writing-plans`)  
- [ ] Then: Phase 1+ code with tests  
