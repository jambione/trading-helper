# Handoff to Grok — night of 2026-10-05 (and 10/6 if needed)

Claude is out of usage for about a day. This is everything needed to run tonight and keep the record clean.
Branch: `master-mac`. The Mac mini (`ssh mac-mini-away`, repo `~/repo/trading-helper`) runs everything.
The mini cannot `git push`: commit there, then fetch and push from the MacBook.

## Hard rules (do not break)
1. **No orders, no trading endpoints, no changes to `config/bot_config.json`, no restarting the desk over ssh**
   (an ssh restart kills the desk's auth; the operator restarts from the mini's Terminal).
2. **Config freeze until about 10/15:** the cost A/B arms are running. No live setting changes, even if a study
   passes.
3. **Pre-registration discipline:** every study below has a pre-registration committed BEFORE data. Do not change
   thresholds, windows or definitions after seeing results. A new idea needs a new pre-registration, committed
   before it runs.
4. **Positive results are NOT final** until Claude's `result-skeptic` reviews them. Write "PENDING SKEPTIC REVIEW"
   on any PASS. Report FAIL / UNDERPOWERED plainly, as they are.
5. **Never print secrets.** Keys live in `config/secrets.json*` on the mini (git-ignored).
6. **No Databento spend.** The operator closed that test; the account has a spend limit.
7. **No calculations on bad data.** If a fetch fails or n = 0, say so; never fill in a plausible number.

## Tonight (after 16:05 ET; SIP data is 15 min delayed)
One command on the mini runs everything (read-only; about 1–2 hours, mostly the replays):

```
cd ~/repo/trading-helper && git pull --ff-only && nohup zsh scripts/night_2026-10-05.sh > /tmp/night.log 2>&1 &
```
Done when `/tmp/rp_lob/done` exists. Outputs are in `/tmp/rp_lob/`.

| # | Output | What it is | Pre-registration / decision rule |
|---|---|---|---|
| 1 | `order_block_gate.txt` (+ `ai_reports/order_block_gate/report.md`) | Do square arms bought inside, or within 0.3% under, a known LuxAlgo resistance order block do worse than other arms? 1,809 arms and 348 fills, 9/11–10/02. | `docs/studies/order_block_gate_prereg.json`. **Only the line "PRIMARY (E, 1m, charted blocks, within 0.3%, day fixed effects)" decides.** PASS = diff ≤ −5 bp, t ≤ −2, ≥ 50 flagged per half, both halves. Under 50 flagged = UNDERPOWERED. Everything else (5m/10m/any-timeframe, inside-only, all-blocks, BREAKOUT, breakout distance bands, ROOM) is information only. |
| 2 | `sr_range.txt` | Operator's "optimal capture": buy the first close up out of a support block (%R rising, < −20), target the bottom of the nearest resistance block (≥ 0.40% room), stop on a close below support, 30-min time stop. | `docs/studies/sr_range_trade_prereg.json`. PASS needs, in BOTH halves: ≥ 50 trades, net > 0 with t ≥ 2, AND beats the same-hour like-for-like control by ≥ 5 bp with t ≥ 2. The room 0.25/0.60 and no-%R lines are information. |
| 3 | `ob_fills.txt` (+ `ai_reports/order_block_gate/heldout_fills.jsonl`) | Today's real desk fills scored under the order-block rule: flagged vs not, realized bp. **This is the held-out record**, the one that will actually decide the rule. | Accumulates one day at a time. Run `.venv/bin/python tools/studies/ob_fills_daily.py YYYY-MM-DD` after every close. |
| 4 | `costing.txt` | Overbought-exit replay: sessions 9/29–10/02 + 10/05, live config vs `ai_exit_left_overbought=true`, costed with real SIP spreads. | Information for a later decision (after the freeze). Previous evidence (9/29 study, 975 fills): the exit made it worse, −0.048R vs −0.044R. Report net bp per trade for both variants, win rate, and n per variant. **Check n > 0 per day first** (the replay has been vacuous before). |

### Write-up
- One doc: `docs/studies/ORDER_BLOCKS_2026-10-05.md`, with the four results in plain words. Each primary verdict
  is copied exactly as printed; information cells are labelled as such.
- Commit on the MacBook (copy files from the mini with `scp`). Add a pointer line at the top of `HANDOFF.md`.
- Caveats to repeat:
  - All four use SIP bars, while the live desk uses the **IEX** feed. Any pass must be confirmed on IEX bars over
    ≥ 10 new sessions before it is a gate.
  - These 16 days have now been mined three times, so the held-out record (#3) is what counts.

## Context from today (for the write-up)
- Operator's hypotheses (10/5):
  - "never buy going into resistance"
  - "a breakout from resistance is a profit run"
  - "room up to resistance is the profit range"
  - "the optimal capture is lift-off from support to the bottom of resistance"
- Port check: `tools/order_blocks.py` (LuxAlgo Order Blocks & Breaker Blocks, swing 10, wicks, charted last 3 per
  side) matched the operator's TradingView charts to the cent on ETHA, EWZ, IHI and XP.
- **Key subtlety:** TradingView draws a block back to its origin candle, but it only *exists* from the bar that
  confirmed it.
  - ETHA's zone (20.49–20.59) was confirmed at 10:35, after the 10:13 entry.
  - EWZ's (43.39–43.95) was confirmed at 08:09, before the 10:11 entry.
- Today's tally (port; "?" = confirm from #3):
  - ETHA 10:13: buy, no zone yet, −$2.66
  - EWZ 10:11: skip, inside, −$1.35
  - GMAB 11:00: buy, nothing overhead, −$1.32
  - IHI 11:11: skip, 0.06% under
  - XP 11:21: buy, 0.42% under (near miss)
  - RXO 12:27: skip?, inside
  - EWZ 12:41: skip?, inside
  - XP 12:41: skip?, 0.03% under
  - GME 11:13: unchecked
- Earlier the same day (all recorded in `docs/studies/` and committed):
  - **Overnight pre-2016 (2000–2015 Yahoo):** the momentum RANKING replicates (+8.6 bp/night, t 9.7, skeptic
    CONFIRMED). SPY-hedged alpha is UNPROVEN. Keep the live overnight account at $100.
  - **Gate grade:** the range-position cap is inert (it delays names about a minute), so keep 90. The gap-down
    block is unconfirmed; re-grade after about 10 sessions.
  - **Stage-2 depth (Databento):** UNDERPOWERED, negative. Premarket depth: closed by the operator, no result.

## Optional, ONLY with the operator's explicit go-ahead: wire the order blocks into the desk as OBSERVE-ONLY
Purpose: build the held-out record on the desk's own **IEX** feed automatically. **No trading behaviour changes.**
- On a **branch** (not master-mac), compute `tools/order_blocks.py` blocks at each arm decision from the desk's
  own 1-minute bars (prior day + today, premarket + RTH), using only blocks known at that moment and the charted
  last 3 per side.
- **Log, never gate.** Add `ob_resist_0.3` (inside or within 0.3% under a resistance block) and `ob_room_pct` to the
  arm/entry event rows. A new knob `ai_watch_ob_observe` defaults to off.
  - It must never refuse, delay or resize an entry.
  - Follow the "indicator whitelist replaces the map" rule: a new field must be copied into the poll's indicator
    dict, or nothing reads it.
- **Cost:** computing blocks over about 800 bars per arm is cheap, but cache per symbol per minute. No new data
  requests: reuse the bars the desk already has.
- **Tests:** a unit test that the flag never changes the arm decision, and one that matches the port on a recorded
  bar series.
- **Deploy:** only if the operator says so. The operator restarts the desk from the mini's Terminal, never over ssh,
  and after the close. Remember that the test suite rewrites `config/bot_config.json`: restore it with
  `git checkout -- config/bot_config.json` after running tests.
- Turning it into a real **skip gate** is a separate decision after about 10/15. It needs a passing pre-registered
  test, about 10 held-out sessions on IEX, and a skeptic review.

## Daily routine (each close, until Claude is back)
1. `.venv/bin/python tools/studies/ob_fills_daily.py <day>`: the held-out order-block record.
2. `.venv/bin/python tools/entry_arm_score.py` and `tools/exit_arm_score.py`: the cost A/B arms (grade about
   10/15).
3. Do not change config. Note anything odd in `HANDOFF.md`.

## Calendar
- About 10/15: grade the entry/exit cost arms (about 10 sessions); the config freeze ends; decide the
  overbought exit using #4 plus new sessions.
- About 10/15–10/19: about 10 held-out sessions of the order-block rule (#3, on IEX bars too). Then
  re-pre-register it as a gate if it holds up.
- After about 10 more sessions: re-grade the gap-down gate (`tools/studies/gate_grade.py`).
- 10/29: the month-end pilot starts scoring (score-only).
