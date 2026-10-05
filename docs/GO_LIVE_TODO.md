# Go-Live To-Do — $250 account path

**Last updated:** 2026-10-05 (Stage 0/1 re-dated: slipped; see below)  
**Rule:** Monday 2026-09-21 is **paper Phase 1**, not live. Do not arm live until Stage 0 passes and paper edge is green.

**Account intent:** $250 Alpaca account for first live dollars. Prefer **margin** (or confirm `multiplier` ≥ 2). Pure cash fights this desk’s turnover (T+1 / good-faith).


---

## Re-dated 2026-10-05 (slipped): what still blocks each stage

Only what is recorded in the repo. Dates are the **earliest realistic** start after the gates, not targets.
(The 9/19 table also had wrong weekdays: "Fri 10/10", "Fri 11/07" are Saturdays; "Mon 10/13", "Mon 11/10",
"Mon 12/08", "Mon 1/05" are Tuesdays.)

**Stage 0: Mon 9/28 → Mon 2026-10-26 (end Fri 2026-11-06).** Reason: freeze to ~10/15, then ops items and the
post-freeze config decisions land the week of 10/19; Stage 0 needs a frozen config for its 2 weeks.
- Config freeze until ~10/15 while the cost A/B arms run (`docs/HANDOFF_2026-10-05_GROK.md`); wiring live keys and
  restarting waits for it.
- **A. Alpaca + Finnhub key rotation**: still open (below; `docs/PHASE1_AFTER_CLOSE_2026-09-21.md` P1 #7).
- **D. Flatten drill, position-close half**: still owed (cancel half PASSED 9/20).
- No $250 desk account created/funded on record. The only live account recorded is the separate overnight test
  account ($100 cash, multiplier 1; `docs/AFTER_CLOSE_2026-10-01.md`).
- Paper Phase 1 fed-book checkpoint is not marked green here. Item F's reconcile cron needed a watchdog restart,
  not recorded as done.
- Post-freeze decisions due ~10/15–10/19: cost-arm grade (~10/15), order-block rule #3 (~10 IEX sessions,
  ~10/15–10/19), forward test #5 (earliest score after the 10/19 close, hard stop 11/02). Any config change they
  cause must land **before** 10/26 or wait until after 11/06, or Stage 0 resets.

**Stage 1: Tue 10/13 → Mon 2026-11-09 at the earliest, CONDITIONAL (end Fri 2026-12-04).** Reason: first Monday
after Stage 0 can pass, AND the paper edge gate must be green, which has no recorded pass, so the date floats.
- Stage 0 pass (deliberate wrong-account assert fires; zero live orders).
- Paper edge gate: last recorded capital gate **2/10** live-positive (need 7/10), pass=False
  (`docs/PROFIT_REDESIGN.md`, `docs/PHASE1_AFTER_CLOSE_2026-09-21.md`); expectancy −0.0421R over 790 closes
  (`docs/GO_LIVE_PLAN.md`). No positive frozen-config window recorded. #5 cannot help before 10/19, and any PASS
  is PENDING SKEPTIC REVIEW and needs its own approval; adopting it means a new frozen window, so Stage 1 slips again.
- Dollar kill not written; $250 account: multiplier/cash-vs-margin, PDT line from Alpaca in writing, accountant on
  wash sales, all open.
- Heartbeat / desk-dead alert (Stage 1's abort rule depends on it), written runbook, UPS: open.
- Phase B in/out of the ramp not decided; 2026-09-16 ledger-hole policy not decided.
- $0 spend: no SIP purchase; the SIP funding gate only matters if SIP = yes.

## Dated agenda (pinned 2026-09-19; Stage rows re-dated 2026-10-05)

Hard date we already locked; Stage dates are **earliest starts** assuming Phase 1 is clean and ops A/D/F get done the same week. A failed stage or mid-stage config change **slips the calendar**.

| When | What | Notes |
|------|------|--------|
| **Mon 2026-09-21** | **Paper Phase 1** fed-book checkpoint | Occupancy / watch mix / thin-book. **No live. No fractional flag flip before open.** Leave-OB lag fix OK if shipped + restarted **before** open. |
| **Mon 9/21 after close → Tue 9/22** | **Paper fractional smoke** | Flip `ai_fractional_shares_enabled=true` on mini, restart, confirm float qty on fractionable names in fill ledger. One paper session is enough. |
| **Week of 9/22** (parallel, not live) | Ops: key rotation · flatten drill · fill-reconcile cron · write $ kill · confirm $250 `multiplier` | Can overlap fractional paper smoke. |
| **Wed 9/23+** (after Phase 1 read) | Paper edge window / Phase 2 baseline if Phase 1 green | Frozen knobs. Live still off. |
| **Earliest Stage 0 start: Mon 2026-10-26** (was Mon 9/28; re-dated 10/05) | Shadow live (~2 weeks) | Live keys on $250 acct, desk still **paper**, prove assert, **$0** risk. Only if Phase 1 OK + keys rotated + flatten drilled. *Why:* freeze to ~10/15, then key rotation, flatten close-half drill and post-freeze config decisions (week of 10/19) must be done first. |
| **Stage 0 end target: Fri 2026-11-06** (was "Fri 10/10") | Stage 0 pass/fail | Wrong-account assert demo + zero live orders. *Why:* 2 weeks from 10/26; hold any config change (incl. a #5 result, hard stop 11/02) until after it. |
| **Earliest Stage 1: Mon 2026-11-09, conditional** (was "Mon 10/13") | Min size, `ai_max_positions=1` (~4 weeks) | First real dollars. Fractionals **on** for this stage (small-account sizing). Pre-commit dollar kill written. *Why:* first Monday after Stage 0 can pass; also needs the paper edge gate green (last recorded 2/10, pass=False), so it floats until that passes. |
| **Stage 1 end target: Fri 2026-12-04** (was "Fri 11/07") | Slippage + reconcile green | Abort on ledger mismatch / unmanaged open. *Why:* 4 weeks from 11/09 (Thanksgiving 11/26 closed, 11/27 half day). |
| **Earliest Stage 2: Mon 2026-12-07** (was "Mon 11/10"; follows Stage 1) | Quarter size (~4 weeks) | Concurrency back; daily brake + EOD exercised. |
| **Earliest Stage 3: Mon 2027-01-04** (was "Mon 12/08"; follows Stage 2) | Half size (~4 weeks) | Runbook interruption test. |
| **Earliest Stage 4: Mon 2027-02-01** (was "Mon 1/05"; follows Stage 3) | Target size | Only after Stages 1–3 all pass. |

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
- [ ] **Stage 0 — Shadow live (~2 weeks)** — earliest Mon 2026-10-26 → Fri 2026-11-06 (re-dated 10/05)  
  Live keys → $250 account · desk still **paper** · prove account assert · **zero** orders routed  
  Pass: deliberate wrong-account assert fires; no live orders
- [ ] **Stage 1 — Min size, `ai_max_positions=1` (~4 weeks)** — earliest Mon 2026-11-09 → Fri 2026-12-04, only if Stage 0 passes and the paper edge gate is green (re-dated 10/05)  
  Measure paper→live slippage; every fill reconciles; software trail closes every open  
  Abort: ledger/broker disagreement, or unmanaged open after desk death
- [ ] **Stage 2 — Quarter size** — concurrency back; daily brake + EOD liquidate exercised
- [ ] **Stage 3 — Half size** — unplanned interruption handled per runbook
- [ ] **Stage 4 — Target size** — only after three consecutive stages pass

### Measurement integrity (found 2026-09-20 by the new reconcile)

- [x] 2026-09-17 reconciles clean: ledger=134 broker=134 matched=134
- [x] 2026-09-18 reconciles clean: ledger=164 broker=164 matched=164
- [x] **2026-09-19 is a SATURDAY** — ledger=0 broker=0 is correct, not an outage. An earlier note in this file called it a no-op Friday and blamed `d7d05b5`; that was a calendar error. Friday was **2026-09-18** (79 buy fills, reconciles exactly). `d7d05b5` tightens entry from 9/18's close onward, so it has never traded a session — `5820c0c` pre-empts it before Monday rather than repairing a loss.
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
