# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-09** (pre-open Wed).

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

1. **Selection rebalance** — Cap momentum seed (esp. `seed_momentum_open_n`), don’t kill. Multi-day source expectancy (fills, MFE≥0.25R, med R/$). Tue: momentum ~39% seed budget, negative $; trending carried +$1.46; NUAI/QBTS also on trending/research so kill overstates benefit.
2. **Entry timing / heating quality** — Prefer heating + RSI≤60 + non-narrowing MACD; less late heat. **Cool EXH↑ arm_ok=0 is a label tautology** (`cooling`=`pctr_falling` cannot be rising under `require_exh_rising`) — redesign/rename later, don’t flip mid-week.
3. **Movers hygiene** — WYHG-class fat spread was Tue’s biggest single $ hole (−$1.28), separate from momentum.
4. **Plan B dry-run scoreboard** — Only if Plan A still flat after leash + tape with clean n.
5. **Bob float/volume pack** — Confirmation tips; not a new strategy.
6. **ATR-scaled trail A/B** — Parked until MFE exists routinely.
7. **Declutter / ledger simplification** — After baselines stabilize.
8. **DiscordOCR.app TCC pin** — Survive Homebrew Python path changes (macOS “access other apps” nag).
9. **Multi-tenant Trader Bro** — After consistent profit only.

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
