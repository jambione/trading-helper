# S/R test queue (Jonathan approved 2026-10-05)

Highest first. Pre-register each item before any data for that item. No `bot_config` trading
changes, no desk restart over ssh, no Databento, no secrets. Stay on study files if observe-only
wiring is WIP on another branch.

| # | Status | Test | Notes |
|---|---|---|---|
| **1** | **DONE — FAIL** ([write-up](SR_EXIT_RESIST_2026-10-05.md); prereg `4aea5ed`) | **Exit-only at resistance** | Resistance TP lost to no-S/R time-stop on both halves. |
| **2** | **DONE — FAIL** ([write-up](SR_SIZE_BY_ROOM_2026-10-05.md); prereg `363728b`) | **Size by room** — half-size when room &lt; 0.40%, full when wider | Sized−equal lift −12.0 / −1.7 bp vs RT ~12 bp. |
| **2b** | **DONE — FAIL** ([write-up](SR_BREAKOUT_CONFIRM_2026-10-05.md); prereg `6b0047e`) | **Breakout confirmation (not the first poke)** | Confirmed−poke lift −19.4 / −16.6 bp vs RT 20 bp; waiting underperforms the poke. |
| **4** | **DONE — FAIL** ([write-up](SR_SUPPORT_ARM_2026-10-05.md); preregs `3255be0` + addendum `afc50c0`) | **Arm earlier on support instead of squares** (+ exit families without desk hold limits) | 15-min: −10.2 / +5.0 bp; exit (b): −22.5 / +23.2 bp vs RT 20. Support fires a median 32 min before the square. Info-only: hold-to-close/long holds beat squares in both halves — candidate for a NEW held-out IEX prereg, not a result. |
| **5** | **ACCRUING — forward test, no score yet** ([prereg md](SR_SUPPORT_HOLD_CLOSE_FORWARD_PREREG.md) / [json](sr_support_hold_close_forward_prereg.json); prereg `c1e9c30`, locked 10/05 before any 10/06 session existed) | **Support-touch arm + hold to 15:55, FORWARD on fresh live-desk IEX sessions (10/06 onward)**, offline/observe-only | Scorer `tools/studies/sr_support_hold_close_score.py` (reads only `ai_reports/sessions/`). Primary control: same name-day same-hour random minute held to 15:55; info: desk's own arms to 15:55, stop/res-touch exits. Cost = max(measured IEX round trip, 20 bp). Pass = lift > cost on BOTH halves, min ≥ 10 sessions & ≥ 100 support arms/half. **Earliest score: after the 10/19 close**; hard stop 20 sessions (~11/02). Until then `--dry-run` counts only. Motivated by the in-sample #4 info cell (+29.4 / +20.7 bp, t 3.7 / 3.3) — NOT evidence. 10/05 excluded (parse dry-run only: 25 support arms found). Any PASS = PENDING SKEPTIC REVIEW. **Addendum `1bd4659` (10/06 03:55 ET, before the open): trailing-stop rows T1–T4, INFO ONLY, cannot change the verdict**; in-sample info: [SR_SUPPORT_TRAILS_INSAMPLE_2026-10-06.md](SR_SUPPORT_TRAILS_INSAMPLE_2026-10-06.md). |
| **3** | **AFTER ~10 IEX sessions** (queued; **do not invent a score yet**) | Score observe-only held-out log (`ob_resist_0.3` / `ob_room_pct`) | No fabricated held-out numbers. **The live window changed on 10/06 (see below): split or flag the log at the first trader restart after 10:14 ET 10/06 (2-date window before, 3-date after). Do not pool them silently.** |

## Live order-block window: 3 sessions (OPERATOR OVERRIDE 2026-10-06 10:14 ET, UNTESTED)
- **Change:** the live `ob_observe` store now keeps the **3 newest ET trading dates** (2 prior sessions + today),
  up from 2 (prior day + today). The setting is the code constant `ob_observe.LIVE_SESSIONS = 3`, **not** a bot_config
  key. Everything else is unchanged: 0.3% band, last 3 blocks per side, swing 10, 1-min IEX 04:00–16:00 ET,
  fail-open, the 10-min stale guard, and the warm fetch feeds only the ob store (no %R side effects).
- **Why:** Jonathan's CRWV paper trade, 10:04:45 ET on 10/06: 4 sh at 91.91, out 91.56, −$1.40. The chart's resistance
  at ~92.03–92.80 comes from the **10/02** open (IEX high 92.78 at 09:33), outside the 2-date window. The desk read 418
  bars (10/05 + 10/06), `ob_room_pct` None and `ob_resist_0.3` False, so the skip correctly allowed the trade.
  Recomputed offline from the recorded 10:03:01 ET CRWV IEX response (fixture
  `tests/fixtures/crwv_iex_1min_20261006_1003.json.gz`): with 3 dates the charted resistance is the 10/02 bear block
  **91.575–91.750**. The 10:04:14 arm price 91.71 is inside it (`ob_resist_0.3` True, room 0.0), so it would have
  skipped; the 91.91 fill is above it. This run did **not** reproduce the chart's 92.03–92.80 zone.
- **Warm fetch:** the first fetch is now 3000 newest 1-min bars (one request; was 2000). Measured 10/06: all 17 first
  fetches of 2000 reached back ~7 IEX sessions (max 392 bars/day), so 2000 already covered 3. 3000 covers the worst
  case of a bar every minute 04:00–20:00 on 3 dates (2880). The `[ob] warm fetch` log line now shows
  `short_window=` (deep fetches that got < 3 dates) and `sessions=3`.
- **Status: UNTESTED.** The skip's pre-registered test (FAILED 10/05) and every S/R study used 2 dates. No study has
  measured a 3-date window. The study tools are unchanged: they share only the block algorithm
  (`tools/order_blocks.py`), not the live store window. The #5 forward-test prereg and scorer are unchanged
  (the scorer reads only recordings).
- Takes effect only when Jonathan restarts the desk from the mini Terminal.

## Hard rules (every item)
- Pre-register (json+md commit) **before** looking at that item's outcomes.
- Measured rates only. Pass bar = beat the locked control before costs by more than the measured
  round-trip spread on **both chronological halves**; also report net after costs.
- Any PASS = **PENDING SKEPTIC REVIEW**. Config freeze until ~10/15 for live trading knobs.
