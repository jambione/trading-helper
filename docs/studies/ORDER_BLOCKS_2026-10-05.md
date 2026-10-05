# Order blocks and the support→resistance range trade: night batch 2026-10-05

Run: `scripts/night_2026-10-05.sh` on the mini, 2026-10-05 16:48–18:13 ET, read-only: no orders, no config edits,
no restarts, no Databento. Raw outputs: copied to [`order_blocks_2026-10-05_raw/`](order_blocks_2026-10-05_raw/) (from `/tmp/rp_lob/` on the mini) (`order_block_gate.txt` is also saved as
`ai_reports/order_block_gate/report.md`; held-out fills in `ai_reports/order_block_gate/heldout_fills.jsonl`).
Pre-registrations: [`order_block_gate_prereg.json`](order_block_gate_prereg.json),
[`sr_range_trade_prereg.json`](sr_range_trade_prereg.json). Both primary tests **FAIL**, so nothing here needs a
skeptic review for a pass.

## Bottom line
- **#2 S/R range trade (the operator's "optimal capture"): FAIL.** It loses after costs in both halves and does
  **not** beat a like-for-like random entry with the same bracket in either half.
- **#1 order-block skip rule: FAIL.** A strong effect in half A (−17.9 bp, t −3.58) is gone in half B (+0.1 bp, t +0.01).
- **#3 held-out fills, day 1 of ~10 (10/05):** flagged −7.4 bp (n 29) vs not flagged −0.4 bp (n 24). One day, not a verdict.
- **#4 overbought-exit costing (information, decided after the freeze):** live config −7.6 bp/trade net (n 369), `ai_exit_left_overbought=true` −7.8 bp (n 385). The overbought exit did not help, matching the 9/29 study.

## 2. Support → resistance range trade (emphasis)

Rule (pre-registered, unchanged): buy the next open after the **first** 1-min close up out of a charted LuxAlgo support
block, 09:40–15:00 ET, not inside resistance, ≥ 0.40% room to the nearest charted resistance above, fast %R below −20
and rising. Target = the resistance bottom; stop = a close below support (out at the next open); 30-min time stop;
0.20% round-trip cost. **Control:** one random minute in the same name-day and same clock hour that is also an up close
with %R below −20 and rising, given the **same % target, % stop and time stop**. That isolates the order-block location
from the bracket shape, the %R condition and the time of day.

Sample: 619 admitted name-days, 16 days 9/11–10/02 ($10+), halves = alternate days. 485 trades, 483 controls.
Drops: no resistance above 801, room below min 428, %R not rising/below −20 374, inside resistance 354;
control fell back to same-hour-only 2, no control 2.

| | Half A (7 days) | Half B (8 days) | All (15 days) |
|---|---|---|---|
| trades | 224 | 261 | 485 |
| **net per trade (after 0.20%)** | **−8.4 bp** (day-clustered t −0.90) | **−20.8 bp** (t −3.73) | −15.1 bp (t −2.88) |
| gross per trade | +11.6 bp | −0.8 bp | +4.9 bp |
| win rate | 45% | 38% | 41% |
| median room to resistance | 1.01% | 1.13% | 1.08% |
| **signal minus like-for-like control** | **−4.2 bp** (t −0.30, n 223) | **−5.2 bp** (t −0.56, n 260) | −4.7 bp (t −0.60, n 483) |

Printed verdict, exactly: **"PRIMARY: FAIL** (bar, both halves: net > 0 with t >= 2, AND beats the same-bracket random
control by >= 5 bp with t >= 2, n >= 50)"

In plain words: lifting off support toward the next resistance block made money **before** costs in half A
(+11.6 bp) but not in half B (−0.8 bp). After the 0.20% cost it lost in both halves (−8.4 and −20.8 bp a trade), and a
random up-close minute in the same hour, with the same target and stop distances, did about as well or slightly
**better** (−4.2 and −5.2 bp). The order-block location adds nothing measurable over the bracket itself. Both
halves have n well above 50, so this is a real FAIL, not UNDERPOWERED.

Information only (not deciding; same sample, so treat as mining):

| info cell | Half A net (t) / minus control (t) | Half B net (t) / minus control (t) |
|---|---|---|
| room ≥ 0.25%, with %R (n 263 / 306) | −9.7 (−1.18) / +6.4 (+0.66) | −20.8 (−4.72) / −5.5 (−0.65) |
| room ≥ 0.60%, with %R (n 181 / 215) | −8.8 (−0.80) / +9.1 (+0.55) | −19.4 (−2.61) / +13.1 (+0.72) |
| room ≥ 0.40%, no %R (n 398 / 461) | −3.9 (−0.56) / +12.6 (+1.70) | −18.5 (−4.43) / −0.3 (−0.04) |

No information cell is net-positive in either half. The best signal-minus-control cell (room ≥ 0.60%, half B
+13.1 bp) has t +0.72.

Caveats: SIP 1-minute bars (the live desk uses IEX); these 16 days were mined on 10/02 for other level features and now
again; flat 0.20% cost rather than per-name spreads; limit fills at the target assumed when the high trades through;
a 30-min time stop, not the desk's trail; %R as defined in the script, not the desk's `_minute_grid_pr`.

## 1. Order-block skip rule (arms inside or ≤ 0.3% under a known resistance block)

E = 1,809 square arms, $10+, 16 days 9/11–10/02; outcome net15 (15 min minus 0.20%); day fixed effects; halves =
alternate days.

| PRIMARY: E, 1m, charted blocks, within 0.3%, day FE | Half A | Half B | All |
|---|---|---|---|
| flagged / arms | 322 / 827 (39%), 8 days | 298 / 982 (30%), 8 days | 620 / 1,809 (34%) |
| net15 flagged vs rest | −19.1 vs −2.2 bp | −13.9 vs −11.2 bp | −16.6 vs −7.4 bp |
| **day-FE diff** | **−17.9 bp, t −3.58** | **+0.1 bp, t +0.01** | −8.7 bp, t −1.58 |
| raw diff (info) | −16.9 (t −3.57) | −2.7 (t −0.29) | −9.2 (t −1.71) |
| winsorized ±300 FE diff (info) | −17.3 | −3.5 | −10.2 |

Printed verdict, exactly: **"PRIMARY (E, 1m, charted blocks, within 0.3%, day fixed effects): FAIL (= no large effect;
a small one cannot be ruled out)** (bar: diff <= -5 bp, t <= -2, flagged n >= 50, both halves)". Also printed:
"Fills, same sign both halves: no (information)".

Information only (selected lines from the output; the full grid is in the report):
- 1m inside-only: A −16.5 (t −3.24), B −6.2 (t −0.40). 1m all-blocks: A −18.0 (t −2.41), B +1.0 (t +0.13).
- 5m and 10m: no line has |t| ≥ 1.5 in half B; the 10m flagged arms are slightly *better* in B (+9.7, t +0.84).
- "Any of 1/5/10m within 0.3%": A −14.1 (t −2.42), B −0.5 (t −0.07).
- Fills F (348, 4 days per half): 1m within 0.3% A +5.8 (t +0.42), B −9.4 (t −0.53); realized flagged −17.1 vs rest
  −12.2 bp (all). 1m inside-only: −31.5 bp (t −1.50) on n 49 flagged.
- BREAKOUT (bearish OB broken upward within 15 min) vs rest: A −7.0 (t −1.05), B −8.7 (t −0.95). Breakouts were
  slightly *worse*, not a "profit run".
- Breakout distance bands (all days): poke −4.1 (t −0.41), clean 0.10–0.30% +14.5 (t +1.54; A +23.6, B +6.5), running
  −21.0 (t −1.33), extended −27.3 (t −1.63).
- ROOM terciles (cuts 0.24% / 0.75% on A): A low −15.4 / mid −16.3 / high −10.5 / none above −4.7; B low −5.1 /
  mid −7.4 / high −22.7 / none above −11.8. More room did not mean better outcomes in B.

## 3. Held-out record: today's real fills under the order-block rule (day 1)
2026-10-05: 53 buy fills. Flagged (inside or ≤ 0.3% under a known resistance block): **n 29, mean −7.4 bp realized**.
Not flagged: **n 24, mean −0.4 bp**. Held-out to date (1 day): the same. The biggest flagged losers were ETHA 10:13
(−68.0), XP 12:41 (−66.1), EWZ 12:41 (−63.5), XP 14:51 (−38.3) and EWZ 10:11 (−34.5); the biggest not-flagged loser was
UPST 13:59 (−65.7). Note: the port flags ETHA 10:13 (room 0.0) while the handoff's chart tally said "no zone yet". This
is worth checking against the chart, because the block's confirmation time decides it. One day; about 10 are needed.

## 4. Overbought-exit replay, costed with real SIP spreads (information only; config freeze)
`tools/replay_session.py` on 9/29, 9/30, 10/01, 10/02 and 10/05: live config (base) vs `ai_exit_left_overbought=true` (lob).
Then `tools/studies/replay_costing.py` charged the full SIP spread at entry once per round trip.
**n > 0 every day:** base 80/56/72/103/58 closed trades, lob 81/60/76/111/57. The overbought exit actually fired in
the lob runs (5/6/16/15/12 `left_overbought` exits). The replay is not empty this time. One base open on 9/30 was on
synthetic data (logged by the replay).

| variant | trades | /day | gross bp | spread bp | **net bp/trade** | t (days) | net $/day @ $1k | win rate after spread* | median net* |
|---|---|---|---|---|---|---|---|---|---|
| base (live) | 369 | 74 | +0.3 | 7.9 | **−7.6** | −4.36 | −56.35 | 39.3% | −3.8 |
| lob (overbought exit on) | 385 | 77 | +0.0 | 7.8 | **−7.8** | −4.67 | −59.71 | 35.3% | −4.9 |

\*Win rate and median are computed from the same cached SIP quotes as the costing script (no new lookups); they are not
printed by it. Gross win rate: base 49.9%, lob 44.7%. Net per day: base −9.1/−7.9/−13.2/−5.1/−3.0; lob −8.8/−9.7/−13.1/
−3.9/−4.4. Both variants lose about the spread, and the overbought exit is slightly worse (−0.2 bp/trade; lower win
rate). That matches the 9/29 study (−0.048R vs −0.044R). Not a config decision before the freeze ends (~10/15).

## Caveats (all four)
- All four use **SIP** bars; the live desk trades on **IEX**. Any future pass must be confirmed on IEX bars over
  at least 10 new sessions before it becomes a gate.
- These 16 days (9/11–10/02) have now been mined three times (10/02 levels study, and tonight's #1 and #2), so
  **the held-out record (#3) is what counts** for the order-block rule.
- With about 7–8 day clusters per half, t ≥ 2 corresponds to roughly p 0.09 per half.

## Next
- Keep running `.venv/bin/python tools/studies/ob_fills_daily.py <day>` after each close (held-out, ~10 sessions).
- The observe-only order-block wiring (operator-approved) has **not** been started; it waits for the operator to read
  these results.
