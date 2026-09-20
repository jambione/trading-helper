# Plan — $250 live → SIP (~$99) → stronger Phase B

**Pinned:** 2026-09-20 with Jonathan  
**North star:** The cash/live **$250** account earns enough to pay **Alpaca Algo Trader Plus (SIP, ~$99/mo)**. SIP then unlocks real premarket tape so **Phase B** (trade 04:00–09:28) can work — and the whole desk gets better data RTH too.

**Names (do not mix):**
| Name | Meaning |
|------|---------|
| **Plan A** | RTH EXH square/triangle desk (what Phase 1 scores) |
| **Plan B burst** | Premarket *universe scout* only → entries still at/after 09:30 (gated off) |
| **Phase B** | Actually trade premarket ~04:00–09:28 (needs prints; SIP is the honest fix) |
| **SIP** | Alpaca Algo Trader Plus ≈ **$99/mo** — full-exchange realtime + usable premarket bars |

---

## What “solid” means here

Three claims, proved in order — not one heroic week:

1. **Worth buying SIP** — historical SIP replay shows Phase B square is computable on the names we admit (already have historical access; $0).
2. **Safe to trade live** — Stage 0 shadow + Stage 1 micro fills on the $250 account (plumbing, not P&L theater).
3. **SIP paid by the desk** — live (or staged) net P&L after the dollar-kill buffer ≥ **$99** in a calendar month, without burning the stake to subscribe.

Chasing (3) before (1)+(2) loses the $250 and still doesn’t fix premarket.

---

## Math (keep visible)

| | |
|--|--|
| SIP cost | ~**$99/mo** |
| Stake | **$250** |
| Need for one month from stake | **+~$99 ≈ +40%** |
| 1R at 1% risk | **~$2.50** |
| Rough R to bank $99 | **~40R net** (before slippage) |
| Paper expectancy (recent) | still slightly **negative** |
| Daily brake | **3R ≈ $7.50** — one bad day is not fatal; a week of them is |

So: SIP-from-$250 is a **multi-week / multi-stage** outcome if edge turns green — not a Mon–Fri sprint. Aspirational old manual ~10%/day would need several clean days *and* no −3R wipe; we have not locked that on paper yet.

---

## Dual track (run in parallel)

```text
TRACK A — Money / live ramp          TRACK B — Prove SIP is worth it
─────────────────────────────        ────────────────────────────────
Mon 9/21  Paper Phase 1              Historical SIP square replay
          occupancy                  (2–3 weeks admits, $0)
Tue+      Paper edge / morning flood Decide: SIP yes / no / later
Ops       keys, flatten, $ kill
~9/28     Stage 0 shadow ($0)        (optional) Finnhub print-path
~10/13    Stage 1 live $250          If SIP=yes → funding gate
          max_pos=1, fractionals
          bank toward $99
When banked ≥$99 past kill           Subscribe Algo Trader Plus
→ enable SIP feed on mini            Phase B paper score on real prints
→ Phase B can graduate               Then Phase B in/out of live ramp
```

**Funding gate (pick one, write it down before Stage 1):**
- **Preferred:** first $99 from **outside** the trading stake (keeps $250 intact); desk later reimburses the goal.
- **Or:** Stage 1+ cumulative realized P&L (after kill buffer) ≥ $99, then subscribe — never subscribe by drawing the stake below kill.

---

## Gate checklist

### Gate 0 — Paper RTH (now → ~late Sep)
- [ ] Mon **2026-09-21** Phase 1 fed-book (morning flood + square arms)
- [ ] Leave-OB / triangle behavior acceptable on a clean day
- [ ] Fractional paper smoke after Phase 1 close
- [ ] Frozen-config stretch with **non-negative** expectancy (don’t arm live on −0.04R hope)

### Gate 1 — SIP evidence ($0) — Track B
- [ ] Replay dual-%R square on **historical SIP** premarket bars for Phase B admits (need ~112 slow bars by 09:20 on most seats)
- [ ] Written go/no-go: “SIP unlocks Phase B seats” vs “still no edge / wrong universe”
- [ ] Do **not** buy SIP on missing-print pain alone without this replay

### Gate 2 — Live plumbing (earliest ~Sep 28)
- [ ] Cash account: read `multiplier`; convert to **margin** if `1` (T+1 fights this desk)
- [ ] Rotate keys · flatten drill · reconcile cron · dollar kill written
- [ ] **Stage 0** (~2 weeks): live keys, desk still paper, assert works, **zero** live orders

### Gate 3 — First live dollars (earliest ~Oct 13)
- [ ] **Stage 1:** `ai_max_positions=1`, fractionals on, measure slippage
- [ ] Abort rules: ledger mismatch, unmanaged open
- [ ] Bank P&L toward SIP only inside Stage rules — no size-up to “make rent”

### Gate 4 — Subscribe + strengthen Phase B
- [ ] Funding gate met ($99 cleared)
- [ ] Flip Algo Trader Plus; point desk feed to SIP where configured
- [ ] Phase B paper scoreboard on **real** prints (fill rate, flat-by-09:28, slip)
- [ ] Only then: decide Phase B inside live ramp (don’t mid-stage flip)

### Gate 5 — Sustain
- [ ] Month 2+ SIP paid from desk surplus *or* consciously from outside capital
- [ ] If live expectancy &lt; 0 for a frozen window → cancel SIP, back to paper (data is a tool, not a sunk-cost trap)

---

## How this strengthens the project

| Without SIP | With SIP (after gates) |
|-------------|------------------------|
| Phase B blind (`phase_b_missing_print`) | Premarket prints/bars → square computable |
| RTH free IEX path only | Fuller tape / less fiction on thin names |
| Plan B burst still off | Optional later: better premarket *scout* too — still not automatic live Plan B |

SIP is **necessary but not sufficient** for Phase B profit. It removes the data choke; edge still has to clear Phase B scoreboard.

---

## Explicit non-goals

- Skipping Stage 0 to “earn SIP this week”
- Buying SIP before Gate 1 replay
- Loosening square arms / raising position ceiling to force $99
- Conflating Plan B burst with Phase B trading
- Paying SIP by draining the $250 below the pre-committed kill

---

## Owner cadence

- **Weekday 7am** go-live morning agenda: where we are on Gates 0–4
- **Monday 8am** open checklist: Phase 1 / desk health
- After each gate: one line in this file dated pass/fail + SHA

**Success one-liner:** *$250 live path funds SIP; SIP turns Phase B from blind to scoreable; Plan A stays the profit engine until Phase B passes its own bars.*
