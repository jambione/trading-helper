# Reframing Lever 1: how could "catch the first leg" make money?

2026-10-30, Claude, at the operator's request ("exhaustive research of how we can reframe lever 1": a different signal, something outside what we have, or tuning what we have).

**Sources:**
- every study in `docs/studies/`;
- the project memory;
- this morning's `first_leg_delay_hist` (VOID: 14.6% of quotes missing; strong but unofficial);
- the plumbing re-measure (`~/replay_runs/lag_breakdown_recent.json`);
- the day-hold forward record.

Outside approaches come from published work I know. I ran no web search, so check citations before relying on them.

## 1. The bar any reframe has to clear

- **Short holds (3 min):** the desk's exits keep 70-78% of a 3-minute move. Break-even needs about **+5 to +11 bp gross** in the first 3 minutes (EXIT_CAPTURE 10/21).
- **Longer holds (10 min):** exits keep only 36-43% of a 10-minute move, so break-even needs about **+24 bp at 10 min**. The alternative is a hold-the-leg exit.
- **Cost:** one round-trip spread on current names is ~3-6 bp, higher at the open (−21 bp net from 09:45 to 10:30 this morning, with no spread cap).

## 2. What the evidence says about the current frame

| Question | Answer | Evidence |
|---|---|---|
| Does the empty square move up after it fires? | No. 3-min gross −0.2 bp (CI −1.0..+0.7); 10-min +0.9 bp | first_leg_delay_hist, move_to_cost_hist (96 sessions) |
| Does entering sooner help? | No. A 2- or 5-minute delay changes gross by < 1 bp | first_leg_delay_hist |
| Is it the plumbing? | Only partly, and it doesn't matter on this signal. Turn-to-fill is 14.3 min: seat wait 5.9 (most of it eviction churn: `never_armable`, `dead_unknown`) and the signal's own wait 8.0 (%R lines, 88%); arm-to-fill is 0.4 min (fixed) | lag_breakdown_recent |
| Any %R timing variant? | No. Turn, pitch, trend trade, pre-square, mid-rise, both-up: all random-minute | prescreen_turn, pitch_*, wr_trend_trade, early_entry_honest |
| Any bar indicator at 3 min? | No. 17 of 18 rules fail; moves are symmetric (±10-12 bp) | THREE_MINUTE_REREAD |
| Tape and book? | Order flow is real but small (+1.5-2 bp, t 3.5); buy-stop +2 bp; sweeps, blocks, book imbalance ~0 | ORDER_FLOW_PILOT, SWEEP_BLOCK, BATCH2 |

**Conclusion:** the problem with the current frame is not speed or plumbing. **The signal carries no direction.** Tuning what we have cannot reach +5 to +11 bp. The best stack of proven pieces (buy-stop + order flow + spread cap) is worth about +4 bp, which gets to roughly break-even, not profit.

## 3. The reframe map

A Leg-1 strategy is five choices. The desk has tested one value of almost every choice, and all the failures share the first two columns.

| Choice | What we've tested | Untested alternatives |
|---|---|---|
| **Names** (which stocks) | The movers / trending board (in-play by screen), ≥ $20 | In play by *catalyst* (earnings, guidance, FDA); in play by *relative volume ranking at the open* (the ORB literature); sector leaders vs laggards |
| **Trigger** (when) | %R squares, pitches, turns; bar indicators | Opening-range breakout (time-anchored); the first pullback after the open drive; VWAP reclaim; prior-day high break |
| **Hold** (how long) | 3-10 min scalps with a tight trail | **Hold the leg: to the close, or to an ATR stop** (the published ORB design) |
| **Data** | IEX (free, live) | Real-time SIP. Several signals showed +4 to +6 bp on SIP and ~0 on IEX (FIRST_PRESQ, %R + RSI) |
| **Side** | Long only (shorts tested: coin flip, squeeze tail) | Market-neutral: long the in-play name, short SPY / sector ETF (removes the market drift that dominated FIRST_PRESQ and day-hold) |

## 4. Candidate reframes, ranked

Odds are my judgment of reaching **net profit after costs in a properly powered test**. They don't come from old-system failures: section 2 uses only today's signal.

| # | Reframe | Why it might work | Evidence so far | Cost to test | Odds |
|---|---|---|---|---|---|
| **R1** | **Stocks-in-play opening-range breakout, hold the leg.** Top ~20 names by relative volume at 09:35, buy a break of the first 5-min high, ATR-based stop, exit at the close | It changes three choices at once (names by RVOL at the open, time-anchored trigger, hold the whole leg). Published: Zarattini, Barbon & Aziz (2024) report Sharpe ~2.4-2.8 over 2016-2023 on US stocks in play. It matches the operator's "first leg" and AGY's "exits must hold the leg" | **Untested, not failed.** Our 10/24 count stopped UNDERPOWERED: one year of data gave an MDE of 27 bp/trade. The fix is history: ~8-10 years of minute bars for only the ~20 in-play names a day | One-time fetch of long history (Alpaca free historical SIP, data key, a few nights), then a prereg | **~20-25%.** Published edges decay and our costs and fills differ, but it is the most structurally different idea with outside evidence |
| R2 | **Market-neutral Leg 1:** any long Leg-1 entry paired with a short in SPY or the sector ETF | Removes the market drift that inflated FIRST_PRESQ (+5.7 bp mostly market) and the day-hold controls | Hedged overnight was ~0 (different horizon). Untested intraday | Cheap (bars we have) | ~10%. A filter on R1 more than a standalone |
| R3 | **Catalyst Leg 1:** earnings / guidance / FDA gap names at the open, first pullback entry | Catalysts create real information flow, unlike screens | News-timed entries failed (+4 gross on SIP); analyst upgrades UNDERPOWERED; earnings drift studied 10/07 (check the doc) | Medium (catalyst calendar + bars) | ~10-15% |
| R4 | **Real-time SIP versions of the current family** | Signals that are +4-6 bp on SIP and ~0 on IEX point to a data gap | FIRST_PRESQ's SIP result was mostly market drift; the SIP breakout failed held-out | Money: SIP ~$99/mo, under "no spend until proven" | ~10% |
| R5 | **Stack the proven small pieces** (buy-stop +2, order flow +1.5-2, spread cap, marketable limit) | Every piece has real evidence | Combined test was too small to judge | Cheap (prereg + recorded data) | ~20% to *break even*, ~5% to profit |
| R6 | **Day-hold picks** (hold 2-3 morning picks all day) | The operator's earlier idea; hold-the-leg | **Forward is failing:** picks −7.8 bp/day vs random +16.8 (11-15 days); 10-yr skeleton FAIL. Read at 40 sessions (~11/13) | Running already | ~5% |
| R7 | **Premarket Leg 1** (T1 sim READY) | Gets in before the open | Premarket spikes fade like RTH; the feed is blind without SIP | Cheap to simulate | ~5-10% |
| R8 | **Fix the seat churn** (`never_armable` / `dead_unknown` evictions that later re-seat and buy) | Operator wants the plumbing fixed; ~26 names evicted then bought later | Speed buys no move on this signal | Config/code + replay | Plumbing win, ~0 bp profit on this signal; matters if R1 or R3 needs fast seating |

Already closed and not worth reopening for Lever 1:
- %R / indicator timing of any kind;
- sideways / slope filters;
- room-below-HOD;
- board-name gates;
- the S/R family;
- Fibonacci;
- ORB on SPY/QQQ (YouTube setups);
- gap-down short;
- halt reopens;
- the fair-value trigger;
- book walls without forward depth.

## 5. Recommendation

1. **Make R1 the Lever-1 research bet.** It's the only reframe that changes names, trigger and hold together, with outside evidence behind it. Our only prior attempt failed on power, not on the result.
   - **Step 1:** fetch ~8-10 years of minute bars for the daily top-20 RVOL names. That's a few nights on the data key, one heavy job at a time.
   - **Step 2:** write a prereg that replicates the published rules, uses our costs, and splits the halves out of sample.
   - **Step 3:** paper forward only if it holds.
2. **Run R2 as R1's filter, not alone:** hedge out the market and check that the edge survives.
3. **Keep R5 (cost stack) moving as Lever 2:** tonight's marketable-limit test is the first piece.
4. **R8 (seat churn) only if R1 or R3 needs it:** on the current signal it buys nothing.

**Honest expectation:** about 20-25% that R1 finds a real, tradeable Leg-1 edge. If it fails on long history with proper power, the entry search on this desk's data has been close to exhausted, and the remaining choice is about the business (paid data, a different market or horizon), not another signal.
