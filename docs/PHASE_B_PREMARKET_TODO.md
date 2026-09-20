# Premarket (Phase B) To-Do

**Last updated:** 2026-09-20

**Work without buying SIP:** [`docs/PHASE_B_WITHOUT_SIP_CLI_BRIEF.md`](PHASE_B_WITHOUT_SIP_CLI_BRIEF.md) — admit tighten, Finnhub prints, ledger hygiene, scoreboard; no Algo Trader Plus.  
**Goal:** Actually trade ~04:00–09:28 ET as an isolated lane (Hybrid C), then score it — without contaminating RTH Phase 1/2.

**Current truth:** Lane is **enabled** (`ai_phase_b_enabled=true`, `ai_phase_b_dry_run=false`) and arm thesis was aligned to RTH square (`d9132fc`), but it has produced **~zero trades**. Dominant refuse: **`phase_b_missing_print`** — free IEX has almost no premarket prints before ~08:00, so the print gate never passes.

---

## Already done

- [x] Session clock: 04:00 start · 09:20 entry cutoff · 09:25 flatten · 09:28 hard flat
- [x] Extended DAY limits + `extended_hours=True` path
- [x] Working-sell exit, chase, software hard stop
- [x] Isolated seats / max opens / ledger tag `phase_b` / no RTH handoff
- [x] Scoreboard tooling (`tools/phase_b_scoreboard.py`)
- [x] Arm path aligned to RTH dual-%R square (not the old 40–70 band opposing RTH)

---

## What it takes to get premarket “up and going”

### 1. Fix the data choke (blocker #1)
Without fresh premarket **prints**, nothing else matters.

- [ ] Confirm live refuse mix still dominated by `phase_b_missing_print` (ledger / scoreboard)
- [ ] Choose data path (pick one, measure first):
  - [ ] **A. Finnhub stream-last** actually feeding Phase B print age (design intent) — verify last prints exist 04:00–08:00 on admitted names
  - [ ] **B. SIP premarket bars** — replay historical SIP for admitted symbols over 2–3 weeks; decide buy only if square is computable (need ~112 slow bars by 09:20 on most seats)
- [ ] Do **not** buy SIP until the historical replay says the square is reachable for the names we admit
- [ ] Fail closed on stale prints (keep ≤15s); never arm on IEX bid/ask fiction

### 2. Prove the lane on paper (after prints exist)
- [ ] Pre-register pass/fail bars **before** the score window
- [ ] Score **only** `phase_b` rows — never mix into RTH Phase 1
- [ ] Track: `n_armed`, fill rate, unfilled/TTL %, entry/exit slip vs last, MFE/capture, `flat_on_time_pct`, peak opens, day P&L
- [ ] Pass requires flat by 09:28 (SOD wipe = fail for that lot)
- [ ] Read `docs/PHASE_B_ALPACA_PAPER_EXTENDED_FILLS.md` — first paper PASS ≠ live edge

### 3. Product / capacity (only after data works)
- [ ] Keep 4–6 Phase B seats, max 1–2 concurrent opens
- [ ] Universe priority: momentum → movers → mention → trending (research/seed_rank off v1)
- [ ] Price floor = desk min (~$2); do not skip sub-$5 just to feel safer
- [ ] Confirm square OB+tight (not legacy 40–70 band) is what arms in logs
- [ ] Optional: `ai_phase_b_legacy_arm` only as rollback

### 4. Ops before any *live* premarket dollars
- [ ] Phase B stays **paper** until RTH Stage 0/1 path is healthy
- [ ] Explicit decision: Phase B inside or outside the live ramp (don’t change mid-stage)
- [ ] Fractional shares: gate to RTH only if shipped — ext-hours limits can’t be fractional
- [ ] Flatten path covers Phase B lots before 09:28

### 5. Known non-goals / traps
- [ ] No Plan B burst conflation (universe scout ≠ Phase B trading)
- [ ] No silent RTH bypass — session flag only
- [ ] No market/bracket/broker-stop outside RTH
- [ ] No retune on the same days you score
- [ ] More premarket data ≠ edge (spikes/fades like RTH) — data only unlocks *whether* we can trade

---

## Suggested order

1. Diagnose print source (Finnhub last vs empty IEX) on one premarket morning  
2. Historical SIP square replay **or** fix Finnhub print path — whichever the diagnosis points to  
3. Paper score window with frozen knobs  
4. Only then: paid SIP (if needed) and any live consideration  

**Bottom line:** Premarket isn’t missing a feature flag — it’s missing **tradable prints**. Plumbing is on; the tape isn’t.
