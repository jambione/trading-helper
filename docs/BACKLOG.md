# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-09** (~10:27 ET; tonight: entry help #1, time-decay #2).

## Operating rules
- **One product lever at a time** (observe/log through RTH; change off-hours).
- **Progress ladder:** clean arms → MFE≥0.25R → capture → med R/$ → equity.
- **Plan A** = stream-fresh + EXH rising (heating) + RSI≤60 + soft_ob/mistimed_heat; local trail; broker stop off.
- **Plan B** = burst trial, code-ready, **live off** until Plan A earns a dry-run open.
- Mid-RTH: no strategy retunes unless on fire (ops OK).

---

## Locked / in flight

| When | Item | Status | Notes |
|------|------|--------|-------|
| **Wed 2026-09-09** | Tight local-trail leash | **LIVE** `11564d3` + follow-ons | `give_r` 0.35→**0.20**, `give_max_pct` 1.75→**1.0**. Synth stop / Plan A arms untouched. Scorecard: `docs/WED_2026-09-09.md` |
| **Wed AM** | AGY seed-rank fix | **LIVE** `736549b` | `agy_model=gemini-3.1-pro-high`; skip `--effort` on `*-high` ids. Prove at ~09:25 seed slot |
| **Thu** | Tape readiness #1 — poller paint-trust parity | **CLI brief ready** | Call `_paint_trust_young_stream_field` before promote on Class C poller + `should_arm_buy` (mirror `apply_tape_blocker`). Tue: only **3** false stale; ~84% true thin |
| **Thu+** | Optional `decision_max_age_sec` 15→20 | Parked behind #1 | Reclaims ~417 RTH border (15–20s) denials |

### Wed scorecard (capture first)
Among fills with **MFE≥0.25R**: median capture clearly better than Tue (aim ≥~40% of MFE; Tue QBTS ~3%). Trail give$ < MFE$ on most local_trails. Pass/fail on capture, not day P&L alone.

### Tue leash counterfactual (optimistic prior)
Same peaks, new leash: **−$0.51 → +$3.38** (+$3.89), all from 6 local_trail exits. Use ~+$3–4 vs Tue as optimistic Wed prior, not a guarantee.

---

## Next (ranked)

1. **Entry help (tonight #1)** — After Wed close score: tighten *what we open*, not the leash. Bundle: (a) refuse / demote late-heat no-MFE (FSLY-class: +% already extended, cm/pctr/macd not ok); (b) heating quality — prefer heating + RSI≤60 + non-narrowing MACD; (c) momentum cap/rebalance (`seed_momentum_open_n`) don’t kill — IRD Wed = MFE 0 / −0.31R poster child; Tue momentum ~39% seed budget, negative $. **Cool EXH↑ arm_ok=0 is a label tautology** — redesign later, don’t flip mid-week. One concrete entry lever for Thu if capture score allows.
2. **Time-decay trail (stall-only) — tonight #2** — After entry help (or if entry slips). While MFE is thin / no new peak, step `local_stop` up toward last on a clock (prefer **5–10s**, not raw 2s); never loosen; floor under last (give_max / min tick); replay must not wreck SMR-class winners. Do **not** stack on live 0.20/1% mid-session.
3. **Movers hygiene** — WYHG-class fat spread was Tue’s biggest single $ hole (−$1.28), separate from momentum.
4. **Thu tape readiness** — Poller paint-trust parity (CLI brief ready); optional ceiling 15→20 after. Still useful; ranks behind entry help for tonight.
5. **Plan B dry-run scoreboard** — Only if Plan A still flat after leash + tape with clean n.
6. **Bob float/volume pack** — Confirmation tips; not a new strategy.
7. **ATR-scaled trail A/B** — Parked until MFE exists routinely (related to time-decay; pick one trail experiment at a time).
8. **Declutter / ledger simplification** — Incl. summary showing floor `stop` instead of live `local_stop` (IONQ-class honesty). After baselines stabilize.
9. **DiscordOCR.app TCC pin** — **Scheduled after Wed 2026-09-09 close (~16:15 ET).** Prefer stable `DiscordOCR.app` binary over Homebrew `Python.app` for OCR so Screen Recording / “access other apps” survives brew upgrades. Do not ship mid-RTH.
10. **Multi-tenant Trader Bro** — After consistent profit only.

---

## Explicitly do not stack now
- Mid-Wed trail + readiness + selection in one day
- Flip `require_exh_rising` / waive `exh_falling` to chase `cool_ok`
- Enable Plan B live / shrink `synth_stop_pct` with the trail lever
- Paid SIP until desk earns it

---

## Evidence pointers (mini)
- Tue baseline score: outcomes + `ai_reports/decision_ledger/2026-09-08.jsonl`
- Ops unstick: `4f5dfd7` SWR `/api/state` + Alpaca pool
- Wed freeze: `docs/WED_2026-09-09.md`
- Seed caps (live): momentum 12 + open 14, trending 20, movers 8, research 12

---

## Kill switch (Plan A)
Trip only if: **clean cool/honest early arms** still show **no MFE** and **neg med R** over enough sessions. Tue did **not** trip (cool_ok=0 by construction; heating arms did print MFE≥0.25R on 3/9).
