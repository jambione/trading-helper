# Trading-helper backlog (desk tracker)

Living list of levers, parks, and evidence. Update when a lever ships or the order changes.
Last updated: **2026-09-16** (half-split dig → ship **mistimed_heat=false** only; full dir-only still parked).

## Operating rules
- **One product lever at a time** (observe/log through RTH; change off-hours).
- **Progress ladder:** clean arms → MFE≥0.25R → capture → med R/$ → equity.
- **Plan A** = stream-fresh + EXH rising in heat band + RSI timer (live still level-capped ≤60 + mistimed/soft_ob; **target** direction-only per Sep-11) + confirm_ticks=1; no MACD arm / no MACD OB pin; local trail; broker stop off.
- **RSI interim:** `cm_rsi_max=60` + soft_ob still **LIVE**; **`mistimed_heat=false` shipped 2026-09-16** (live-effective slice of narrower PASS cell — see Next #2). Full dir-only (`rsi_max=null`) still **parked** (`half_ok=False`). Do **not** soften EXH / heat band / `require_exh_rising`. Do **not** auto-apply `exh_heat_min20`.
- **Plan B** = burst trial, code-ready, **live off** until Plan A earns a dry-run open.
- Mid-RTH: no strategy retunes unless on fire (ops OK).
- **admit_ledger** = refused-side instrument (`ai_reports/admit_ledger/YYYY-MM-DD.jsonl`, uncapped seed + inclusion). Observe-only; does not change who gets in. Gate grading (select/neutral/invert) after ≥1–2 RTH days of rows. Occupancy churn stays the weekly primary.
- **proposal_ledger** = attributed proposal instrument (`ai_reports/proposal_ledger/YYYY-MM-DD.jsonl`, kept+dropped, proposer≠owner, state-change+5m heartbeat). Additive to admit_ledger. **source_scorecard** grades suppliers after ≥1–2 RTH days of ledger (GROSS + controls); no live demote/cap until pre-registered rules fire with adequate n.

---

## Locked / in flight

| When | Item | Status | Notes |
|------|------|--------|-------|
| **Tue 2026-09-16 after RTH** | admit_ledger (refused seed+inclusion) | **LOCAL → deploy after close** | Logging-only; fail-open; knobs on. Prefer alone if sharing a restart with occupancy A2/A3 would confuse attribution. |
| **Tue 2026-09-16 after RTH** | proposal_ledger + source_norm + source_scorecard | **LOCAL → deploy after close** | Observe-only; additive to admit_ledger; state-change+5m heartbeat. Scorecard nightly via watchdog. No live seed-cap/demote in this pack. |
| **Mon 2026-09-15 after RTH** | Lean Plan A three-knob | **LIVE** | `confirm_ticks=1`; `ob_allow_flat_when_macd_armed=false`; `require_realtime_macd=false`. **Wed measure:** fewer `arm_confirming`, ~zero `overbought_macd_armed`, same/more clean EXH↑+RSI arms → occupancy dig if still thin. |
| **Wed 2026-09-09** | Tight local-trail leash | **LIVE** `11564d3` + follow-ons | `give_r` 0.35→**0.20**, `give_max_pct` 1.75→**1.0**. Synth stop / Plan A arms untouched. Scorecard: `docs/WED_2026-09-09.md` |
| **Wed AM** | AGY seed-rank fix | **LIVE** `736549b` | `agy_model=gemini-3.1-pro-high`; skip `--effort` on `*-high` ids. Prove at ~09:25 seed slot |
| **Thu** | Tape readiness #1 — poller paint-trust parity | **SHIPPED** | `_paint_trust_young_stream_field` on Class C poller + `should_arm_buy` (mirror `apply_tape_blocker`). Age ceiling 15→20 still parked. |
| **Thu+** | Optional `decision_max_age_sec` 15→20 | Parked behind #1 | Reclaims ~417 RTH border (15–20s) denials |
| **Thu** | Green catch-up time-decay trail | **LIVE** (enable true) | `ai_local_trail_time_decay_enabled=true`, idle 8s, step 0.05R; overlay on give_r 0.20/1%; score capture vs early-scratch; flip off Fri if runners taxed |
| **Wed 2026-09-16 after RTH** | RSI direction-only (`rsi_dir_only_fall_gt10`) | **FAIL — no ship** | AB 9/11–9/16: mean lift +0.085% but `half_ok=False`. Kitchen-sink parked. |
| **Wed 2026-09-16 after RTH** | Half-split dig → mistimed off | **SHIP mistimed=false** | Dig: Wed 9/16 tanked dir-only half B (−1.72% day lift); `rsi_extended` dominated LIVE-blocked keeps. Narrower AB PASS: `rsi_dir_mistimed_off_keep_max60`. Live ship = **mistimed off only** (fall=10 not live-wired when `require_rising=false`). `allow_falling_below` stays 20; soft_ob stays on; max60 stays. Fri kill: flip mistimed back if no MFE/arrived-hot lift. Artifact: `benchmarks/entry_ab/half_split_dig_2026-09-11_2026-09-16.md`. |

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

1. **Wed measure three-knob lean Plan A** — fewer `arm_confirming`, ~zero `overbought_macd_armed`, same/more clean EXH↑+RSI arms. If still thin → occupancy dig (not RSI/EXH soften).
2. **RSI timing — mistimed off LIVE; full dir-only PARKED** — Half-split dig 9/11–9/16: half A=`[9/11,9/14]` half B=`[9/15,9/16]`. Dir-only pain = **Wed 9/16** day lift −1.72% vs LIVE; LIVE-blocked keeps dominated by **`rsi_extended`** (A×95 / B×149) — nulling max floods ledger. Narrower PASS: `rsi_dir_mistimed_off_keep_max60` n=35 mean=+0.879% lift=+0.687% half_ok=True (lift_a=+0.20 / lift_b=+0.93). **Shipped:** `ai_watch_mistimed_heat_enabled=false` only (operator: live-effective path). **Not shipped:** `allow_falling_below=10` (harness-only while `require_rising=false`), `rsi_max=null`, soft_ob off. Caveat: lone `rsi_mistimed_off` cell was AB FAIL (lift=−0.12) — Fri score is kill switch. Optional follow-up: align `cm_rsi_allows_buy` falling floor with harness `gate_keep` so fall=10 is live-testable. Dig: `benchmarks/entry_ab/half_split_dig_*.md`.
3. **Entry help — heat_max 65–70** — Parked behind Wed three-knob + RSI dir measure unless EXH band is the proven leak.
4. **Green catch-up trail (time-decay)** — **SHIPPED ON for Thu.** Idle 8s / step 0.05R / green-only / raise-only. Score capture vs early-scratch Fri; kill switch = set enabled false if runners taxed.
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
