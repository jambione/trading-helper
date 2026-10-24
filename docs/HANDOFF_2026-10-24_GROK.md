# Handoff to Grok — 2026-10-24 (Sat) ~06:30 ET

Claude is out of usage. This brief is what you need to pick up safely. Read `docs/running_list.md` (rows 18-26) and
`docs/TEST_SCORECARD_2026-10-23.md` for the full history.

## 1. Ground rules (the operator's standing rules — do not break)
- **Desk config is FROZEN** for the floor20 forward window (restarts **Mon 10/26**, tag `floor20-window-2`, commit
  `891d9b5b` on the mini). No entry/exit/size/floor/code change without the operator's explicit OK, and never during
  market hours (default to after 16:00 ET).
- **Data fetches for studies use the data-only Alpaca account** (`secrets_data.json`), never the desk key. Reading the
  desk's own paper orders read-only with its key is fine (as `floor20_forward.fills()`), after hours only.
- **No spend** (no paid data) unless the operator approves; they will only pay if a free historical test on the same
  data shows it matters.
- **Never enter or paste credentials.** **Do not push** to GitHub unless asked. The mini cannot git push (commit on the
  mini, fetch from the MacBook).
- **Process for any test:** pre-register (docs/studies/*_prereg.json) → adversarial review (max 2 rounds) → build →
  **planted-edge canary** (inject +10 bp after entry; the harness must recover it) → outcome-blind **count step**
  (floors + MDE) → score only if powered → write up. Never read the held-out `warehouse_confirm` (2026-01-14 .. 04-24)
  except to confirm a PASS.
- Ask before killing processes on the mini; never run replays/backfills on the desk's API key during market hours.

## 2. Where the research stands (entry edge)
- ~60+ entry tests since September; **none passed.** Every family fails the same way: gross ≈ 0 ± 2 bp, net ≈ −cost.
  All 10 study harnesses pass the planted-edge canary and real desk fills match the SIP bars (87% inside the minute bar,
  the rest +1.5 bp above = buying the ask), so the failures are real, not bugs.
- **Two real-but-small effects** (both ~+2 bp, under the 3-5 bp round-trip cost):
  1. **Buy-stop at the oversold minute's high** (batch 2 T2): +2.0 / +2.4 bp vs random (t 5.0 / 8.7), gross +1.9/+2.8.
  2. **Buyer-initiated SIP order flow** (order_flow_pilot G1): +1.5-2 bp, t ~3.5.
- **Lag audit (1,247 desk buys):** the arm fires ~**14 min / +24 bp after** the IEX %R turn; fill is 11 s; feed lag 0 s.
  The bottleneck is the arm rule's waiting — but entering *at* the turn by rule is still a random minute
  (operator's %R trend trade FAIL on 485k trades).
- Exits are fine (keep 70-78% of a fast edge); gates protect (refused names do worse).
- Results: `docs/studies/BATCH2_2026-10-24.md`, `WR_TREND_TRADE_2026-10-24.md`, `CANARY_2026-10-23.md`,
  `VIDEO_SETUPS_2026-10-23.md`, `RETAIL_ORB_COUNT_2026-10-24.md`, `CCI_2026-10-23.md`.

## 3. In flight — pick these up
### A. `buystop_flow` (buy-stop entries filtered by buyer flow, at quoted cost; arm A %R exit, arm B desk exits)
- Prereg `docs/studies/buystop_flow_prereg.json` — **READY** (two rounds + conditions checked; config pinned
  `837dfd79268ebf16`, R = 0.05009, no exit knob changed vs the exit study's config).
- Code `tools/studies/buystop_flow.py` — code review round 1 returned **FIX FIRST**; NOT yet applied:
  1. Arm-B last chunk must end at `min(T + MAX_HOLD + 60, 15:50)` (a time-cap exit otherwise trips the score guard).
  2. The 98.75% upper bound must use `t_ppf(0.9875)`, not `0.99375`.
  3. Merge a second split across a chunk boundary in `secs` (lowest low, later last price).
  4. Count arm-B drops when `fetch_bid` returns None.
  5. Add ai_positions / bars / config / leash_replay / eod_reversal / indicator_confirm_hist to `module_shas`.
  6. Make the probe run the real `fetch_one` + `fetch_arm_b` on 10+ stride-spread trades (fetch_bars, walk CPU included).
  7. Implement or declare-dropped the G2/G3, exit-spread-by-side and SPY-hedged cells; $10-$20 cell is impossible
     on a ≥$20 sample (declare).
  8. Budget: expect ~66k requests (~7.4 h); N may need to drop to ~6,500/half (projected MDE ~2.9 bp vs 3 bp bar).
  9. Empty trade-condition lists count as round lots (`codes or [" "]`): declare or align with the pilot.
- After fixes: send to the reviewer for round 2, then on the mini **from `~/repo/trading-helper/tools/studies`** (the
  desk-exit code imports the desk modules; dependencies are already copied there as untracked files):
  `--probe` → `--fetch` → `--cuts` → count → `--score --skeptic-ready "<READY>" ` (prereg commit b519b9fc).
- `sample.json` is built on the mini (15,950 trades, 7,975/half, all stops re-derived).
- Expectation: best case ~0-1 bp net (likely NOT PASSED / UNDERPOWERED). If it fails, the operator agreed to **stop
  hunting minute-scale entries on free data.**

### B. Day-hold forward logger (the one running longer-hold test)
- It had **stopped logging after 10/21**: the nightly run asked for SIP daily bars ending 23:59 UTC "today", which the
  plan refuses ("recent SIP"). Fixed in `tools/studies/name_history.py` (commit "name_history.daily_bars: cap the
  request end 16 min before now"), copied to the mini; a catch-up `day_hold.py forward` was running at handoff.
  Verify `ai_reports/day_hold/forward.json` now includes 10/22 and 10/23 (last read: 9 days, picks −32 bp/day, t −2.2;
  read at 40 sessions ~11/13).

## 4. Calendar
| When | What |
|---|---|
| Mon 10/26 | floor20 window restarts (operator's 5 settings live: wr_trend_min_rise 15, movers $20 / 50 / 50 / 50); `~/studies_bin/floor20_forward.py --log` runs 16:30 weekdays via cron |
| ~10/27 | price30 read |
| 10/29-30 | first month-end SPY flow score (the one live flow lead, +20 bp historically) |
| ~11/2 | support hold-to-close forward read |
| ~11/13 | day-hold forward read (40 sessions) |
| ~early Dec | floor20 read (≥ 20 sessions, ≥ 150 fills) |

## 5. Open issues
- **Nightly exact replay** blows up to ~14 GB at startup (since ~10/15); the 6 GB cap now kills it cleanly
  (10/23 night) — root cause not fixed.
- Untracked study files sit in the mini's repo `tools/studies/` (copied for buystop_flow); they are in git on the
  MacBook — a `git pull` on the mini will reconcile.
- Nothing from 10/23-10/24 is pushed; local commits only (MacBook `master-mac`).

## 6. The operator's position (10/24)
Pessimistic after the long run of failures; wants a stopping rule. Options discussed: (1) longer holds — open to it,
but overnight lost ~0.8%/night on paper and is parked; (2) paid data — only with proof it matters; (3) run the desk on
paper and let the forward tests report. Recommended: (3) now, decide on (1) after buystop_flow and the forward reads.
