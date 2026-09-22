# After-close worklist — Phase 1 paper

Living list. Add items during RTH; implement **after close** only (no mid-session retune / no arm loosen).

**Standing locks:** square OB+tight enter · leave-OB triangle exit · trail backup only · prefer-square admit · morning flood 09:30–11 · paper only. Oversold-triangle arm **off** for the 2026-09-22 session (`6fd643e`).

---

## Still left — after close Tue 2026-09-22

Written locally, not on the mini: 30s catch-up (`entry_catchup_stop`, `ai_local_trail_entry_catchup_sec=30`) and triangle flatten (`merge_triangle_indicator`, engine fast-left wins over a live read still in the square). Tests passed. Not committed, not shipped. A restart waits until after the close.

1. **Ship the exit changes.** Commit, pull on the mini, restart outside RTH. That is the 30-second park at last − $0.01 when the fill shelf has not moved, and the square exit when the fast line the book is showing has left ■.

2. **Seat names on the way into the square — written, not shipped.** Morning flood was seating every momentum name, including far ones, and that filled the book before a pre-square name could keep a seat. Flood now keeps square / pre_square only while prefer-square is on. Far momentum does not take a keep seat. This does not open a new buy.

3. **Do not open before the square.** Replay of Mon 9/21 and Tue 9/22: first pre-square entry versus first fresh square. At 15 minutes, pre-square was median **−0.12%** (35 trades) and the fresh square was median **+0.01%** (58 trades). Pre-square does not beat the square book, so there is no early open. The chart's green run is not a better entry than waiting for ■.

4. **MACD gap arm — analyzed, written, not shipped.** On Mon 9/21 and Tue 9/22, names that were not in a square, the best 15-minute rule was: MACD bullish, gap rising, gap at least **0.02% of price**, CM RSI **rising and under 60**. 15 entries, median **+0.20%**, mean **+0.15%**, 60% winners. Wider gaps and RSI at 70+ were flat to down. RSI not rising was a loser (median −0.20%). Built as `macd_gap_fill_allows_buy`, live flag `ai_watch_macd_gap_arm` in bot_config. It does not override a fresh square. Ships with the exit changes after the close. The sample is 15 trades and the typical dip (−0.39%) is larger than the typical 15-minute gain, so the first live day is the check, not a proof.

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
