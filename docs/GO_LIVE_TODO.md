# Go-Live To-Do — $250 account path

**Last updated:** 2026-09-20  
**Rule:** Monday 2026-09-21 is **paper Phase 1**, not live. Do not arm live until Stage 0 passes and paper edge is green.

**Account intent:** $250 Alpaca account for first live dollars. Prefer **margin** (or confirm `multiplier` ≥ 2). Pure cash fights this desk’s turnover (T+1 / good-faith).


---

## Dated agenda (pinned 2026-09-19)

Hard date we already locked; Stage dates are **earliest starts** assuming Phase 1 is clean and ops A/D/F get done the same week. A failed stage or mid-stage config change **slips the calendar**.

| When | What | Notes |
|------|------|--------|
| **Mon 2026-09-21** | **Paper Phase 1** fed-book checkpoint | Occupancy / watch mix / thin-book. **No live. No fractional flag flip before open.** Leave-OB lag fix OK if shipped + restarted **before** open. |
| **Mon 9/21 after close → Tue 9/22** | **Paper fractional smoke** | Flip `ai_fractional_shares_enabled=true` on mini, restart, confirm float qty on fractionable names in fill ledger. One paper session is enough. |
| **Week of 9/22** (parallel, not live) | Ops: key rotation · flatten drill · fill-reconcile cron · write $ kill · confirm $250 `multiplier` | Can overlap fractional paper smoke. |
| **Wed 9/23+** (after Phase 1 read) | Paper edge window / Phase 2 baseline if Phase 1 green | Frozen knobs. Live still off. |
| **Earliest Stage 0 start: Mon 2026-09-28** | Shadow live (~2 weeks) | Live keys on $250 acct, desk still **paper**, prove assert, **$0** risk. Only if Phase 1 OK + keys rotated + flatten drilled. |
| **Stage 0 end target: Fri 2026-10-10** | Stage 0 pass/fail | Wrong-account assert demo + zero live orders. |
| **Earliest Stage 1: Mon 2026-10-13** | Min size, `ai_max_positions=1` (~4 weeks) | First real dollars. Fractionals **on** for this stage (small-account sizing). Pre-commit dollar kill written. |
| **Stage 1 end target: Fri 2026-11-07** | Slippage + reconcile green | Abort on ledger mismatch / unmanaged open. |
| **Earliest Stage 2: Mon 2026-11-10** | Quarter size (~4 weeks) | Concurrency back; daily brake + EOD exercised. |
| **Earliest Stage 3: Mon 2026-12-08** | Half size (~4 weeks) | Runbook interruption test. |
| **Earliest Stage 4: Mon 2027-01-05** | Target size | Only after Stages 1–3 all pass. |

**Premarket (Phase B):** parallel track — print-path fix → paper score → maybe SIP. **Not** on the Stage 1 critical path; decide in/out of ramp before Stage 1 starts.

**Fractional testing fit:** unit tests ✅ (PR #29) · paper smoke **after Monday close / Tuesday** · live only from **Stage 1** onward (not Stage 0).

---


## Short-term north star (pinned 2026-09-19)

**Goal:** $250 live book funds **Alpaca Algo Trader Plus / SIP (~$99/mo)** from trading profit.

**Math check (do not paper over):**
- Target ≈ **+$99** ≈ **+40%** on $250 in ~5 sessions.
- At 1% risk, 1R ≈ **$2.50** → need on the order of **~40R net** in a week.
- Paper expectancy is still ~**−0.04R** — this is a stretch north star, not a Stage 0/1 pass criterion.
- Old manual clean days ~10%/day were aspirational; even that needs ~4 green days in a row without a −3R brake day wiping the month.

**Compatible with the dated agenda (not instead of it):**
1. **Mon 9/21** — Phase 1 paper (no live, no SIP buy required).
2. **This week (parallel, $0 risk):** historical SIP square **replay** for Phase B admits (access already in hand per GO_LIVE §3.1). Decide buy/no-buy from that score — do **not** subscribe on hope.
3. **SIP purchase:** only after (a) replay says premarket square is reachable, **and** (b) either outside capital covers $99 **or** live Stage 1+ has actually banked ≥$99 *without* putting the kill switch at risk. Prefer funding SIP outside the $250 trading stake so data cost doesn’t eat the experiment.
4. **Do not** skip Stage 0 or arm live Mon–Fri next week just to chase the $99. Plumbing failures cost more than one month of SIP.

**Success definition for “SIP from desk”:** first calendar month where live (or paper-proven then live) net P&L after the dollar kill buffer ≥ $99 *and* Phase B/RTH scoreboard still passes. Week-one is the *intent*, not the gate.

**Full plan:** [`docs/SIP_FROM_250_PLAN.md`](SIP_FROM_250_PLAN.md) — dual track (live ramp + SIP replay), gates 0–5, funding rule, Phase B unlock.


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
- [~] **D. Flatten drill** — **cancel path PASSED 2026-09-20** on paper `PA3VCF6H9RXG`: resting SPY limit placed, `tools/flatten.py --yes` found `0 position(s) … 1 open order(s)`, cancelled, verified flat, exit 0. Confirmed independently at the broker (`CANCELED`, `filled_qty=0`, 0 orders / 0 positions) rather than trusting the tool's own "✓ Flat".
  **Still owed: the position-close half.** Market was closed, so nothing could fill and the sell path was never exercised. Run with a real fill on **Tue 2026-09-22**, alongside the fractional smoke — not Monday, which is the Phase 1 scoring session.
- [x] **F. Fill-reconcile cron** — wired into `tools/watchdog.py` after `daily_learn`, shouts on nonzero rc (`08fee09`). The tool could not have worked unattended before: `reconcile_day()` reads the broker via `alpaca_trader`, which is only `is_active()` after the **desk** calls `init()`, so every cron run returned `trader_off`. It now connects for itself via `flatten.resolve_credentials()`. **Needs a watchdog restart on the mini to take effect.**
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

### Measurement integrity (found 2026-09-20 by the new reconcile)

- [x] 2026-09-17 reconciles clean: ledger=134 broker=134 matched=134
- [x] 2026-09-18 reconciles clean: ledger=164 broker=164 matched=164
- [x] 2026-09-19 broker confirms ledger=0 broker=0 — Friday's no-op was real, not a logging failure (cause: `d7d05b5` tightened entry; fixed in `5820c0c`)
- [ ] **2026-09-16 has a ledger hole: ledger=49 broker=80, 31 `BROKER_ONLY`** (FPS/RETO/LUXE/RUM). The fill ledger shipped 2026-09-18 and that day was backfilled incompletely. **Do not score 2026-09-16** — any expectancy or source-scorecard run covering it is reading ~60% of the fills. Decide: backfill it properly or exclude the day by policy.

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
