# After-close worklist — Phase 1 paper

Living list. Add items during RTH; implement **after close** only (no mid-session retune / no arm loosen).

**Standing locks:** square OB+tight enter · leave-OB triangle exit · trail backup only · prefer-square admit · morning flood 09:30–11 · paper only. Oversold-triangle arm **off** (`6fd643e`).

**How to use:** drop bullets under **Scratch (today)** during the session. At close, promote into **Do tonight** / P0–P2, ship, then move shipped items into the dated **Shipped** section.

---

## Do tonight — Wed 2026-09-23

### P0 — must ship / score

1. **RTH first-buy at 09:40 ET (operator call mid-session Wed)**  
   Do **not** auto-buy at the 09:30 bell. Target first new entries **09:40**.  
   **Today:** no live flip mid-session.  
   **After close:** add an explicit buy-gate (proposed `ai_rth_buy_start_time="09:40"`) in `trading_hours_active` so Alpaca `market_open` alone is not enough. Keep watch/shadow + morning-flood **seating/warm** from 09:30; only **arms/fills** wait until 09:40. Move `ai_open_bell_time` to **09:40** (or later) so overnight open-bell path does not jump the gun (live default is 09:35). Fingerprint the new key. Tests: before 09:40 → no buy; at/after 09:40 + market open → allowed.  
   **Why:** SNXX 09:30:26 fill → dead follow-through → trail −0.19R by 09:32; open auction noise is a bad first print for square scalp.

2. **SNXX triangle-first fail-open (local, uncommitted)**  
   Shelf tick with blank dual %R returned `no_dual_read` and let `local_trail` sell while `exh_was_overbought` (SNXX 09:30–09:32, −0.19R, zero `local_trail_deferred_*`).  
   **Fix in tree:** `no_dual_read_hold` + entry-features fallback + persist `pos["indicator"]` on manage + `now=` in `apply_local_trail`. Tests: `tests/test_trail_triangle_first.py` (12 pass).  
   **After close:** commit → deploy mini → `./trading restart` → confirm next square seat logs `local_trail_deferred_dual_ob` when dual is blank/still OB.

3. **Phase 1 / capital card for Mon–Wed**  
   Occupancy (open≥1/≥2/≥3) · exit race `left_overbought` vs `local_trail` · MFE≥0.15R share · live-equivalent session green count.  
   Capital gate (mini 10d): **2/10** live-positive · med MFE−spread ≈ 0 · **pass=False**. Do not arm live on this.

4. **SNXX day note** — one closed trade today at open: trail sold into dead follow-through; ▼ never fired (still square). Attribute to (2), not to “triangle broken.”

### P1 — ops before Stage 0

5. **Flatten drill — position-close half** (cancel path already PASSED 2026-09-20). Need a real fill then `tools/flatten.py --yes` + broker verify.  
6. **Fractional paper smoke** — confirm `ai_fractional_shares_enabled` path on a fractionable fill (or explicitly defer with date).  
7. **Rotate Alpaca + Finnhub keys** (still open on `docs/GO_LIVE_TODO.md`).  
8. **Dollar kill line** written before any Stage 1 dollar.

### P2 — measure / don’t jump

9. **SIP Gate 1 OOS** — ≥5 sessions / plateau ~2.5–3e6; verdict GO/NO-GO/LATER. No subscribe from thin in-sample.  
10. **TV vs engine ■** — side-by-side %R on 1–2 names when mismatch recurs.  
11. **Go-live morning agenda** — failed ~7:21 on 2026-09-21; re-test weekday 7am land.

---

## Scratch (today — Wed 2026-09-23)

- **09:30 SNXX:** entry_ok square → `local_trail` at 09:31:58 · no `left_overbought` · no deferral log · `exit_exhaustion=None` · `extension_class=dead_follow_through`. Root cause: triangle-first `no_dual_read` fail-open (see Do tonight #2).
- **Operator:** wait on the bell — first buys at **09:40** (see Do tonight #1). No mid-session config flip.
- Screenshots around 09:42 showed **NUAI** chart chrome; closed trade under discussion was **SNXX**.
- Book flat as of ~09:43 ET after SNXX exit.

---

## Shipped after close Tue 2026-09-22 — `8b6d271`

Live on the mini after the close restart.

1. **Exits.** 30s catch-up parks an unmoved shelf at last − $0.01 (`ai_local_trail_entry_catchup_sec=30`). Triangle flatten uses the engine fast leave over a live read still in the square (`merge_triangle_indicator`).
2. **Seating.** Morning flood keeps only square / pre_square (and os_*) seats while prefer-square is on. Far momentum does not take a keep seat.
3. **No early open.** Mon/Tue replay: first pre-square median −0.12% (35) vs first fresh square +0.01% (58). No pre-square open shipped.
4. **MACD gap second arm.** Live: bull, gap rising, gap ≥ 0.02% of price, RSI rising and under 60, and the name is not in a fresh square (`ai_watch_macd_gap_arm`).

### Also shipped (ops / OCR)

- DiscordOCR Screen Recording denial → `discord_ocr_health.json` + watchdog CRITICAL; GUI-only recovery (`scripts/enable_ocr_capture.command`).
- Square sticky ■ / admit upgrade path (`3278c75` + follow-on unknown→square freeze).

---

## Carry-forward (still open from Mon/Tue list)

| # | Item | Status |
|---|------|--------|
| A | Full-day Phase 1 occupancy card | Open — score Mon–Wed tonight |
| B | Exit race trail vs triangle | Partial — triangle-first shipped; **SNXX hole** = tonight #1 |
| C | No-extension journal fields | Shipped; keep measuring, do not loosen square |
| D | Journal clarity (`arm_why` on buys/board) | Open |
| E | Discord outbound guard on other type-ticker paths | Confirm after close |
| F | SIP Gate 1 OOS | Open — earliest Wed 9/23+ |
| G | Live / $250 / fractional | Plumbing only until Stage 0 gates |

**CLI brief:** [`docs/PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md`](PHASE1_PRESQUARE_TRAIL_TRIANGLE_CLI_BRIEF.md)  
**Go-live ramp:** [`docs/GO_LIVE_TODO.md`](GO_LIVE_TODO.md)
