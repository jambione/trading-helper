# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-09** (~11:24 ET; #1b clears MACD-gap arm states too).

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

## After close 2026-09-09 (locked path)

1. **Score Wed capture** under live `give_r=0.20` / `give_max_pct=1%` (MFE≥0.25R median capture vs Tue; not day P&L alone).
2. **Entry help #1 — ship `ai_watch_exhaustion_heat_max_pct` 65–70** (now 0/off). Blocks FSLY-class EXH 72; keeps Wed winners (EXH≤55). Do **not** require cm/pctr/macd_ok.
3. **Declutter #1b — remove macd-gap from arms, states, watchlist** — turn off `ai_watch_macd_block_narrowing` (stop treating `macd_min_gap` as an arm veto). `require_macd` already false. Skim Wed’s `macd_gap_narrowing` blocks first so Thu arm flood isn’t a surprise. **Align all surfaces:** retire gap/narrowing from arm_why → ~8 veto buckets, decision_ledger, desk legend; **clear watchlist State reasons that are MACD-gap/narrowing clutter** (e.g. `macd_gap_narrowing` / “MACD closing” style arm states tied to gap); remove MACD-gap column/chip from dashboard watchlist. Don’t leave zombie MACD-gap states operators still read. Scope = **arm/state/UI for gap**; don’t silently gut separate MACD *exit* paths in the same ship unless they’re already dead. Bearish block stays a *later* IRD tool. **Not** the entry-quality fix (heat_max is).
4. **Exit #2 — green catch-up time-decay** (design agreed): when `last > entry`, on ~8s idle raise `local_stop` toward `last − min_cushion` (small `0.05R` steps, raise-only). Banks pause-in-green instead of price falling to stop. Defaults off → replay Tue+Wed → enable. Does not replace entry help for day $.
5. Parked same night unless score demands: tape readiness (Thu), momentum cap, summary-vs-local_stop honesty, DiscordOCR pin (~16:15).

## Next (ranked)

1. **Entry help — heat_max 65–70** — Tonight after Wed score. See after-close path.
2. **Green catch-up trail (time-decay)** — Tonight #2 after heat_max. Stop rises to the gain when last > entry; idle ~8s; step 0.05R; floor under last; decay off when not green. Replay before enable.
3. **Movers hygiene** — WYHG-class fat spread was Tue’s biggest single $ hole (−$1.28), separate from momentum.
4. **Thu tape readiness** — Poller paint-trust parity (CLI brief ready); optional ceiling 15→20 after.
5. **MACD declutter** — drop narrowing/gap arm veto after heat_max (#1b). Bearish block / momentum cap stay separate follow-ons.
6. **Plan B dry-run scoreboard** — Only if Plan A still flat after leash + tape with clean n.
7. **Bob float/volume pack** — Confirmation tips; not a new strategy.
8. **ATR-scaled trail A/B** — Parked (pick one trail experiment: green catch-up first).
9. **Declutter / ledger simplification** — Incl. summary showing floor `stop` instead of live `local_stop`.
10. **DiscordOCR.app TCC pin** — **~16:15 ET Wed 2026-09-09.** Prefer `DiscordOCR.app` over Homebrew `Python.app`.
11. **Multi-tenant Trader Bro** — After consistent profit only.


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
