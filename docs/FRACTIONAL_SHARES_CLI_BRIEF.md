# CLI brief — Fractional shares (RTH sizing + orders)

**Repo:** `jambione/trading-helper` · base `master-mac`  
**Scoped:** `docs/GO_LIVE_PLAN.md` §4.1 (2026-09-19) — **not built**.  
**Why now:** $250 live path needs intended risk (~1%) without whole-share truncation chewing ~30% of size on sub-$5 names.

## Product

| Case | Behavior |
|------|----------|
| Symbol `asset.fractionable == true` | Size and submit **float qty** (Alpaca notional floor ~$1) |
| Not fractionable / lookup fail / transient API error | **Fail closed → whole shares** (today’s behavior) |
| Phase B / any `extended_hours=True` order | **Whole shares only** — Alpaca forbids fractional ext-hours |
| Broker stops / brackets / OTO | Stay off for fractional path (`ai_broker_stop_enabled=false` already) |

- RTH entries today are `market` / DAY — fractional-capable.
- Exits: local trail / EOD flatten are RTH market closes — OK. Do **not** leave a fractional lot resting on an ext-hours limit sell.
- Coverage expectation: ~77% of recent traded names fractionable; thin microcaps stay whole-share.

## Current gaps

1. `desk_risk.size_long_from_free_equity` — eleven `int(... // ...)` truncations (~451–511); `FreeEquitySize.qty: int`; return `qty=int(qty)`.
2. `desk_risk.cap_long_qty` / other sizers — `int(qty)` / `int(... // px)`.
3. `alpaca_trader` — `qty = int(amount // price)` (~552, ~650); submit paths `qty=int(qty)` (~1384/1415+).
4. No `symbol_fractionable()` beside `symbol_tradable()` asset cache (~289+).
5. Ledger / outcomes may assume int `total_qty` in places — audit and allow float.

## Implement

### A. Config flag

- Add `ai_fractional_shares_enabled: true` (default **false** until tests green; ship default **true** for paper once suite passes, or leave false and enable on mini deliberately).
- Document in `config.py` SAFE/default map.
- When false: behavior byte-identical to today (all ints / whole shares).

### B. Asset gate (`alpaca_trader`) — small

1. Add `symbol_fractionable(ticker: str) -> bool` next to `symbol_tradable`.
2. Read `asset.fractionable` from the same Alpaca asset fetch / cache pattern as tradable.
3. Cache **definitive** True/False only; on miss / exception / no client → **False** (whole shares).
4. Never treat “unknown” as fractional.

### C. Sizer (`desk_risk.size_long_from_free_equity`) — main work

1. Change `FreeEquitySize.qty` (and related qty fields used as shares) to `float`.
2. When fractional permitted for this symbol (caller passes `fractional: bool` **or** sizer accepts a flag):
   - Replace `int(x // px)` with floor-to-reasonable precision (e.g. round down to 0.001 or Alpaca’s allowed precision — verify current Alpaca share precision; do not over-round).
   - Prefer **qty from notional/risk dollars ÷ price**, then apply the same clamps (free_cap, concentration, slot, cheap, risk_ceil, buying_power) in **share space** or notional space consistently.
3. Replace `min_share` / `qty == 0 → 1` floor with **minimum notional ~$1** (Alpaca floor): if computed notional &lt; $1 and free equity allows, bump to $1/px (fractional) or 1 share (whole).
4. When `fractional=False`: keep exact current int truncation + 1-share floor.
5. Update `cap_long_qty` similarly (float path vs int path).
6. Grep all `FreeEquitySize` / `.qty` consumers (`ai_entry_watch`, `ai_trading`, tests) — accept float; do not re-`int()` away fractionals on the buy path when enabled.

### D. Order placement (`alpaca_trader`) — small

1. Helper `_order_qty(qty, *, fractional: bool) -> float|int`:
   - fractional → positive float, reject if &lt; min notional / invalid
   - else → `int(qty)` with existing `&lt; 1` skip
2. Replace bare `int(amount // price)` and submit `qty=int(qty)` on **buy** paths used by the AI desk.
3. **Hard gate:** if `extended_hours=True` or Phase B entry/exit limit path → force whole shares even if flag on.
4. Log `fractional=true|false` on BUY notes for scoreboard.

### E. Exits / Phase B / ledger

1. Verify `close_position` / liquidate / local trail market flatten accept fractional qty (Alpaca does).
2. Phase B: **never** size or submit fractional (ext-hours limits).
3. EOD ~15:50 flatten must remain inside RTH so fractional lots always have a market exit.
4. Ledger / fill reconcile / outcomes: store qty as float; R math uses float shares × dollars.

### F. Tests

- Fractionable + enabled: target 2.93 sh at $0.852 → qty ≈ 2.93 (not 2); effective risk ≈ 1.0% not 0.68%.
- Not fractionable: still whole shares (2).
- Flag off: identical ints to pre-change fixtures.
- Ext-hours / Phase B path: always int qty.
- Asset lookup failure → whole shares (fail closed).
- Min notional: sub-$1 computed size bumps or skips cleanly (no zero-qty submit).
- `cap_long_qty` / free_equity clamps still bind (20% cap, free_cap, bp).

### G. Ship

- Branch off `master-mac`, PR, merge.
- Pull mini; restart stack (sizer + trader bind at runtime — restart to be safe).
- Leave `ai_fractional_shares_enabled=false` on first deploy if you want a dark ship; then flip true on paper and watch one session’s sizes in the fill ledger.
- Do **not** arm live as part of this PR.

## Done when

1. Paper RTH buys on fractionable names show float qty in broker + ledger.
2. Non-fractionable + Phase B remain whole-share.
3. Fail-closed on asset errors.
4. Suite green; mini restarted; one logged fractional fill and one whole-share fill.

## Explicit non-goals

- Enabling live trading / Stage 0.
- Fractional broker stops or brackets.
- Fractional extended-hours / Phase B orders.
- Raising `ai_risk_pct` / position caps “because fractionals exist.”
- Spending this day instead of print-path / edge work if Monday Phase 1 is the priority — §4.1 sequencing note: this multiplies whatever edge you have; build when you’re ready to use the $250 path, not as a substitute for expectancy.

## Suggested commit title

`feat(size): fractional shares for RTH when asset.fractionable`
