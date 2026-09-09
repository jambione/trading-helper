# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-09** (~16:00 ET; #1b MACD-gap declutter shipped — narrowing off).

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
3. **Declutter #1b — remove macd-gap from arms, states, watchlist** — **SHIPPED** — `ai_watch_macd_block_narrowing=false` (stop treating `macd_min_gap` as an arm veto). `require_macd` already false. Skim Wed’s `macd_gap_narrowing` blocks first so Thu arm flood isn’t a surprise. **Align all surfaces:** retire gap/narrowing from arm_why → ~8 veto buckets, decision_ledger, desk legend; clear MACD-gap State clutter; remove MACD-gap column/chip from dashboard watchlist. Scope = arm/state/UI for gap; don’t gut separate MACD *exit* paths same ship. Bearish block = later IRD tool.
4. **Occupancy dig (tonight, no knob unless dig screams one)** — seed→admit→seat→arm→open conversion; vision = fed book → **2+ concurrent opens all day**. Wed noon: n_book=1, long flat occupancy. Output: ranked causes + at most **one** candidate lever for Thu — don’t ship movers/mom-cap/bearish/reseed cool in the same breath as heat_max.
5. **Thu tape — paint-trust poller parity (ship tonight)** — CLI brief already ready; optional age ceiling 15→20 only if parity alone isn’t the story. Live for Thu RTH.
6. **Exit #2 — green catch-up time-decay** — when `last > entry`, ~8s idle raise `local_stop` toward `last − min_cushion` (0.05R steps, raise-only). Defaults **off** → replay Tue+Wed → enable (code can land tonight; don’t enable live until replay).
7. **Same-night side:** DiscordOCR.app pin ~16:15.
8. **Explicitly NOT same-night ships:** movers hygiene, momentum cap, `macd_block_bearish`, occupancy knobs (reseed cool / dead_reentry / TOD trending floor) — dig/notes OK; ship after Thu sees heat_max + tape. Summary-vs-local_stop honesty stays parked.

## Next (ranked)

1. **Entry help — heat_max 65–70** — Tonight after Wed score. See after-close path.
2. **Green catch-up trail (time-decay)** — Tonight #2 after heat_max. Stop rises to the gain when last > entry; idle ~8s; step 0.05R; floor under last; decay off when not green. Replay before enable.
3. **Movers hygiene** — WYHG-class fat spread was Tue’s biggest single $ hole (−$1.28), separate from momentum.
4. **Thu tape readiness** — Poller paint-trust parity (CLI brief ready); optional ceiling 15→20 after.
5. **All-day occupancy (watchlist → 2–4 concurrent opens)** — Jonathan’s vision: a couple of open positions going *all day*, not a 9:30 burst then a dead book. A 1-name watchlist cannot feed that. Wed ~12:10: **n_book=1** (GME), **0 opens**, occupancy ~3 names ~10:00 then **flat until POET 11:34–11:58**. Seeds (trend/movers/research/mom) are on; seats aren’t — diagnose the *conversion* (seed → admit → seat → arm → open), not “add more seeders.” Known thinners: `dead_reentry` (max 1/symbol/day), stale/no-tape drops, admit gates, midday %/score/rvol, batched research, mom seats 0. Dig after heat_max + MACD-gap declutter. **Not** mid-RTH.
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
