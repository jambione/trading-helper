# CLI brief — Phase B / premarket: everything except buying SIP

**Repo:** `jambione/trading-helper` · base `master-mac`  
**Locked with Jonathan 2026-09-20:** Do all Phase B readiness work that does **not** require purchasing **Alpaca Algo Trader Plus / live SIP (~$99)**. Historical **delayed** SIP bars for studies are allowed (already free).

**Out of scope (hard):** subscribe / enable live `data_feed=sip` · pay for SIP from the $250 · claim Phase B is “live profitable.”

**In scope:** admit-universe so historical SIP clear-112 hits the **go** bar · Finnhub stream-last actually feeding prints · ledger/scoreboard hygiene · re-run Gate 1 replay · paper readiness when prints exist.

Related: `docs/PHASE_B_PREMARKET.md`, `docs/PHASE_B_PREMARKET_TODO.md`, `docs/SIP_FROM_250_PLAN.md`, `docs/SIP_HISTORICAL_REPLAY_CLI_BRIEF.md`, `tools/phase_b_sip_replay.py`.

---

## Current truth (do not re-diagnose)

- Phase B plumbing on; **~zero fills**.
- Dominant refuse: **`phase_b_missing_print`** (Finnhub last missing/stale; IEX useless 04:00–08:00).
- Gate 1 mini run 2026-09-20: SIP clear-112 **45.9%** / IEX **0%** → verdict **LATER** (feed choke real; buy bar not met).

---

## Work packages (ship in this order)

### WP1 — Admit universe for SIP-computable names (no purchase)

**Goal:** Raise historical SIP `clear_112` on the Phase B admit set from ~46% to **≥60%** (Gate 1 go bar), without paying for live SIP.

1. From `ai_reports/phase_b_sip_replay/` day JSONs, list admits that **failed** clear-112 (thin: TNMG/SBUX-class) vs **passed** (CRWV/RKLB/NFLX-class).
2. Change Phase B seat/admit policy:
   - Prefer **momentum / movers** with a **liquidity floor** (min SIP bars proxy: price ≥ `ai_watch_min_price` ($2), optional min prior-day dollar volume or min premarket bar count from delayed SIP when available).
   - Cap or demote chronic non-clearers; keep 4–6 seats, max 1–2 opens.
   - Research/seed_rank: **off for Phase B v1** if they drag clear rate down (RTH morning flood stays separate).
3. Re-run on mini:
   ```bash
   .venv/bin/python tools/phase_b_sip_replay.py \
     --ledger-dir ai_reports/phase_b_ledger --days 15 \
     --out ai_reports/phase_b_sip_replay/
   ```
4. **Done when:** summary verdict **go** (or documented “still later” with new % and next admit tweak). Still **do not subscribe**.

### WP2 — Finnhub stream-last print path (free live path)

**Goal:** `phase_b_missing_print` is rare when Finnhub actually has a last — so we can arm on free data where SIP bars aren’t required for the *print gate* (square still needs bars; this unblocks *something* to evaluate).

1. One premarket morning (or replay stream logs): for admitted symbols 04:00–08:00, log Finnhub last age vs refuse reason.
2. Fix wiring if last exists but Phase B doesn’t see it (subscribe set, symbol normalize, age clock, wrong feed key).
3. Keep `ai_phase_b_print_max_age_sec=15`; fail closed; never arm on IEX bid/ask.
4. **Done when:** ledger shows a material drop in `phase_b_missing_print` on names Finnhub prints — or a written finding “Finnhub also empty 04:00–08:00 → SIP required for prints too.”

### WP3 — Ledger / seats_full hygiene

**Goal:** Readable ledgers (9/18 had 100k+ `phase_b_seats_full` admits_refuse noise).

1. Don’t spam `admit_refuse` every poll when seats are full — sample or edge-trigger.
2. Ensure admit kinds used by `phase_b_sip_replay` stay stable (`admit`, `dry_armed`, …).
3. **Done when:** a morning ledger is reviewable; replay still finds admits.

### WP4 — Scoreboard ready (no fills yet)

**Goal:** When prints appear, scoring is one command — bars already frozen in `PHASE_B_PREMARKET.md`.

1. Confirm `tools/phase_b_scoreboard.py` fixtures still pass.
2. Pre-register the next paper score window dates **before** any knob changes.
3. Document: paper extended fills are optimistic (`PHASE_B_ALPACA_PAPER_EXTENDED_FILLS.md`).
4. **Done when:** checklist says “run scoreboard on days D1–Dn” with no retune mid-window.

### WP5 — Ops alignment (still no SIP buy)

- Phase B stays **paper** until RTH Stage 0/1 healthy.
- Decide Phase B in/out of live ramp **before** Stage 1 (don’t mid-stage flip).
- Fractionals remain **RTH-only** (ext-hours limits can’t be fractional).
- Flatten covers Phase B lots by 09:28.

---

## Explicit non-goals

- Purchasing or enabling **live** SIP / Algo Trader Plus.
- Plan B **burst** live enable.
- Loosening RTH square arms / Monday Phase 1 contamination.
- Retuning go/no-go bars after seeing replay to force a “go.”
- Mixing Phase B P&L into RTH Phase 1 gates.

---

## Done when (overall, still without paying)

1. Gate 1 replay is **go** on an tightened admit set **or** Finnhub path proven viable for prints with a written SIP decision still deferred.
2. `missing_print` understood and reduced where Finnhub has data.
3. Scoreboard + ledger hygiene ready for the first printable week.
4. `docs/SIP_FROM_250_PLAN.md` Gate 1 updated — subscribe still unchecked until funding gate.

## Suggested commit titles (per WP)

- `feat(phase_b): admit liquidity floor for SIP-computable names`
- `fix(phase_b): Finnhub last wiring for premarket print age`
- `fix(phase_b): quiet seats_full admit_refuse spam`
- `docs(phase_b): pre-register score window (no SIP)`

## Suggested first PR

**WP1** (admit tighten + re-run replay) — unblocks the SIP *purchase decision* without spending $99.
