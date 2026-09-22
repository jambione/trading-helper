# After-close worklist — Phase 1 paper

Living list. Add items during RTH; implement **after close** only (no mid-session retune / no arm loosen).

**Standing locks:** square OB+tight enter · leave-OB triangle exit · trail backup only · prefer-square admit · morning flood 09:30–11 · paper only. Oversold-triangle arm **off** for the 2026-09-22 session (`6fd643e`).

---

## Shipped after close Tue 2026-09-22 — `8b6d271`

Live on the mini after the close restart.

1. **Exits.** 30s catch-up parks an unmoved shelf at last − $0.01 (`ai_local_trail_entry_catchup_sec=30`). Triangle flatten uses the engine fast leave over a live read still in the square (`merge_triangle_indicator`).
2. **Seating.** Morning flood keeps only square / pre_square (and os_*) seats while prefer-square is on. Far momentum does not take a keep seat.
3. **No early open.** Mon/Tue replay: first pre-square median −0.12% (35) vs first fresh square +0.01% (58). No pre-square open shipped.
4. **MACD gap second arm.** Live: bull, gap rising, gap ≥ 0.02% of price, RSI rising and under 60, and the name is not in a fresh square (`ai_watch_macd_gap_arm`). First live day is the check; the sample was 15 trades.

---

## P0 — thesis / Phase 1 score

**CLI brief (paste-ready):** [`docs/PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md`](PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md) — aggressive pre-square farm + trail/triangle + no-extension journal.

1. **Full-day Phase 1 occupancy card** — open≥1 / ≥2 / ≥3 minutes + square/pre/far mix. Morning flash (~66% ≥1, thin ≥2/≥3, flat between bursts) is not the day score.
2. **Exit race: trail vs triangle** — **ROOT CAUSE (Mon tape):** live `arm_r=be_at_r=0.15`, `min_hold=90`; day close mix `local_trail` 11 · `left_overbought` 5 · fill-through 2; **8/11 trail closes still had exit_exh≥80** (still-OB scale) while leave-OB logged **76 min_hold deferrals**. Trail/BE flattened into square hold. Fix: triangle-first suppress in `apply_local_trail` (MAE≤−1R escape only).
3. **No-extension after square** — 12/18 never MFE≥0.15R. Journal now stamps `hit_trail_arm` / `extension_class` / `arm_why` / seat class admit+fill. Do **not** loosen square gate.
4. **Pre-square pipeline (aggressive farm)** — shipped: `max_far=0`, `far_evict=45s`, active `far_exh_steal_for_{pre_}square`, flood far stealable, board `n_square/n_pre_square/n_far`. `include_pre` stays false.

## P1 — observability / ops

5. **Journal clarity** — buys log seed text (`mom+trending`) not arm class; `warming` looks like heat. Surface `arm_why` / dual-%R gap / square vs heating on fills & board.
6. **TV vs engine ■** — operator saw non-squares; engine shadow said 12/12 dual-OB+tight. Side-by-side %R length/scale/lag on 1–2 names (NUAI/CRML).
7. **Discord outbound** — shipped `a4ea203` ADD_TV abort if Discord frontmost. After close: same guard on any other type-ticker path (e.g. CREATE_TV_ALERT); confirm no webhook send.
8. **Go-live morning agenda routine** — failed 2026-09-21 (~7:21). Fix / re-test so weekday 7am agenda actually lands.

## P2 — roadmap (don’t jump)

9. SIP Gate 1 still **THIN** — no subscribe until OOS ≥5 sessions (~Wed 9/23+).
10. Live / $250 / fractional — plumbing only; not today’s Phase 1 work.

---

## Scratch (add below during session)

- **OCR Screen Recording flake (10:17 ET):** DiscordOCR lost Screen Recording → silent fail; SSH/Terminal relaunch cannot grant TCC. Harden: document GUI-only restart; ensure DiscordOCR.app + Terminal/Ghostty stay enabled; optional watchdog that surfaces Screen Recording denial instead of quiet backoff.

- **Square false-■ / admit gap (12:58 ET):** PSKY sticky ■ — shipped `3278c75`. Follow-on: APLD 13:33 `admit=unknown` while fill=`square` — freeze was locking unknown before dual-%R arrived; fix `maybe_freeze_exh_seat_class_admit` (unknown may upgrade).
