# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-10** (~08:37 ET; occupancy follow-up after Thu score).

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
| **Thu** | Tape readiness #1 — poller paint-trust parity | **SHIPPED** | `_paint_trust_young_stream_field` on Class C poller + `should_arm_buy` (mirror `apply_tape_blocker`). Age ceiling 15→20 still parked. |
| **Thu+** | Optional `decision_max_age_sec` 15→20 | Parked behind #1 | Reclaims ~417 RTH border (15–20s) denials |
| **Thu** | Green catch-up time-decay trail | **LIVE** (enable true) | `ai_local_trail_time_decay_enabled=true`, idle 8s, step 0.05R; overlay on give_r 0.20/1%; score capture vs early-scratch; flip off Fri if runners taxed |

### Wed scorecard (capture first)
Among fills with **MFE≥0.25R**: median capture clearly better than Tue (aim ≥~40% of MFE; Tue QBTS ~3%). Trail give$ < MFE$ on most local_trails. Pass/fail on capture, not day P&L alone.

### Tue leash counterfactual (optimistic prior)
Same peaks, new leash: **−$0.51 → +$3.38** (+$3.89), all from 6 local_trail exits. Use ~+$3–4 vs Tue as optimistic Wed prior, not a guarantee.

---

## After close 2026-09-09 (locked path)

1. **Score Wed capture** under live `give_r=0.20` / `give_max_pct=1%` (MFE≥0.25R median capture vs Tue; not day P&L alone).
2. **Entry help #1 — ship `ai_watch_exhaustion_heat_max_pct` 65–70** (now 0/off). Blocks FSLY-class EXH 72; keeps Wed winners (EXH≤55). Do **not** require cm/pctr/macd_ok.
3. **Declutter #1b — remove macd-gap from arms, states, watchlist** — **SHIPPED** — `ai_watch_macd_block_narrowing=false` (stop treating `macd_min_gap` as an arm veto). `require_macd` already false. Skim Wed’s `macd_gap_narrowing` blocks first so Thu arm flood isn’t a surprise. **Align all surfaces:** retire gap/narrowing from arm_why → ~8 veto buckets, decision_ledger, desk legend; clear MACD-gap State clutter; remove MACD-gap column/chip from dashboard watchlist. Scope = arm/state/UI for gap; don’t gut separate MACD *exit* paths same ship. Bearish block = later IRD tool.
4. **Occupancy — dig done + Thu lever:** Wed thin book was not empty seeds — `dead_reentry` (1147) locked IRD/ODD/FSLY/GME while seed_rank kept feeding them; tape_only/stale seats secondary. Jonathan: if name still has volume/value, **seat it and let arms decide** (heat_max/RSI/stream), don’t all-day bench on a morning scratch. **Ship `ai_dead_reentry_block=false` for Thu** (config). Soften later only if 0-MFE rebought. Dig notes kept under Next #5.
5. **Thu tape — paint-trust poller parity** — **SHIPPED** — optional age ceiling 15→20 only if parity alone isn’t the story.
6. **Exit #2 — green catch-up time-decay** — **LIVE for Thu** (`ai_local_trail_time_decay_enabled=true`, idle 8s, step 0.05R). When `last > entry`, idle raise `local_stop` toward `last − min_cushion` (raise-only overlay on give_r 0.20/1%). Score capture vs early-scratch; flip off Friday if runners get taxed.
7. **Same-night side:** DiscordOCR.app pin ~16:15.
8. **Explicitly NOT same-night ships:** movers hygiene, momentum cap, `macd_block_bearish`, occupancy knobs beyond dead_reentry (reseed cool / TOD trending floor) — after Thu scores dead_reentry off + tape. Movers / mom-cap / bearish still parked. Summary-vs-local_stop honesty stays parked.

## Next (ranked)

1. **Entry help — heat_max 65–70** — Tonight after Wed score. See after-close path.
2. **Green catch-up trail (time-decay)** — **SHIPPED ON for Thu.** Idle 8s / step 0.05R / green-only / raise-only. Score capture vs early-scratch Fri; kill switch = set enabled false if runners taxed.
3. **Movers hygiene** — WYHG-class fat spread was Tue’s biggest single $ hole (−$1.28), separate from momentum.
4. **Thu tape readiness** — Poller paint-trust parity **SHIPPED**; optional ceiling 15→20 after if needed.
5. **All-day occupancy follow-up (after Thu 2026-09-10 score)** — Goal: ≥2 concurrent opens as session health via armable seats × overlapping holds. Do **not** loosen heat_max/RSI/EXH or slow green catch-up just to fill slots. Next levers if Thu still single-file after 10:15: (1) evict/churn unarmable `tape_only`/stale seats so liquid names get seats, (2) midday seed refresh (trending % / reseed cool after ~10:30). Score today's dead_reentry-off first.
6. **MACD declutter** — drop narrowing/gap arm veto after heat_max (#1b). Bearish block / momentum cap stay separate follow-ons.
7. **Plan B dry-run scoreboard** — Only if Plan A still flat after leash + tape with clean n.
8. **Bob float/volume pack** — Confirmation tips; not a new strategy.
9. **ATR-scaled trail A/B** — Parked (pick one trail experiment: green catch-up first).
10. **Declutter / ledger simplification** — Incl. summary showing floor `stop` instead of live `local_stop`.
11. **DiscordOCR.app TCC pin** — **~16:15 ET Wed 2026-09-09.** Prefer `DiscordOCR.app` over Homebrew `Python.app`.
12. **Multi-tenant Trader Bro** — After consistent profit only.


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
