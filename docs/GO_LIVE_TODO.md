# Go-Live To-Do — $250 account path

**Last updated:** 2026-09-19  
**Rule:** Monday 2026-09-21 is **paper Phase 1**, not live. Do not arm live until Stage 0 passes and paper edge is green.

**Account intent:** $250 Alpaca account for first live dollars. Prefer **margin** (or confirm `multiplier` ≥ 2). Pure cash fights this desk’s turnover (T+1 / good-faith).

---

## Already done (do not re-open)

- [x] §2.1 Unauthenticated remote config/credential write — fixed + deployed
- [x] §2.3 Fill ledger
- [x] §2.4 Two-factor live arm (`ai_live_trading_enabled` + `config/live_armed.json` + account assert)
- [x] §2.5 Staleness guards fail closed
- [x] Independent flatten switch (`tools/flatten.py`)
- [x] Daily loss brake restored (`ai_daily_loss_limit_r = 3.0`)
- [x] Live pinned off (`ai_live_trading_enabled = false`; no `live_armed.json` on mini)
- [x] Secret Scan baseline audited

---

## Before any live dollar

### Ops / safety
- [ ] **A. Rotate Alpaca + Finnhub credentials** (old §2.1 hole was public)
- [ ] **D. Flatten drill** — open a small *paper* position, run `tools/flatten.py --yes`, confirm broker flat
- [ ] **F. Fill-reconcile cron** — wire `tools/fill_reconcile.py` post-close; alert on nonzero exit
- [ ] Heartbeat / desk-dead alert when positions open
- [ ] Written runbook (crash with positions, API down, Finnhub dead, mini unreachable)
- [ ] UPS on mini; no auto OS updates during RTH

### Account / money ($250)
- [ ] Create/fund the $250 live account **separate** from anything else
- [ ] Read `multiplier` from Alpaca: `1` = cash (settlement constraints), `2`/`4` = margin (matches desk)
- [ ] If cash: convert to margin **or** accept hard settled-cash / low round-trip policy
- [ ] Get **one written line from Alpaca** on PDT under $25k (FINRA PDT gone; broker policy still matters)
- [ ] Pre-commit a **dollar kill** that ends the experiment and returns to paper (write it down before Stage 1)
- [ ] Accountant touch on wash sales if staying high-frequency
- [x] *(Optional but high leverage at $250)* Ship fractional shares (§4.1) — built; `ai_fractional_shares_enabled` default **false** (dark ship; flip on paper deliberately)

### Ramp (§6) — config frozen inside each stage
- [ ] **Stage 0 — Shadow live (~2 weeks)**  
  Live keys → $250 account · desk still **paper** · prove account assert · **zero** orders routed  
  Pass: deliberate wrong-account assert fires; no live orders
- [ ] **Stage 1 — Min size, `ai_max_positions=1` (~4 weeks)**  
  Measure paper→live slippage; every fill reconciles; software trail closes every open  
  Abort: ledger/broker disagreement, or unmanaged open after desk death
- [ ] **Stage 2 — Quarter size** — concurrency back; daily brake + EOD liquidate exercised
- [ ] **Stage 3 — Half size** — unplanned interruption handled per runbook
- [ ] **Stage 4 — Target size** — only after three consecutive stages pass

### Edge gate (independent of plumbing)
- [ ] Paper Phase 1 fed-book checkpoint green (Mon 2026-09-21+)
- [ ] Frozen-config window with **positive** expectancy (today still ~−0.04R — not a live gate)
- [ ] Do **not** loosen square OB+tight or raise position ceiling to “fix” capacity

---

## Standing rules once live

- Daily brake stays on at `3.0R`
- Never scale into a failed stage
- Two-factor arm only on the trading machine; config push alone must not arm
- Premarket Phase B is a **separate** lane — do not fold into live RTH Stage 1 until its own scoreboard passes
