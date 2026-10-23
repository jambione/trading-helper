# Test scorecard: every study run, 2026-09-26 .. 10-23

**Sources:**
- result docs in `docs/studies/` (108 write-ups, 127 preregs);
- the memory notes that summarise studies without a doc of their own.

**How studies are counted:**
- One row per study. A study that tested many cells (e.g. 540 threshold cells) is one row.
- "Passed" means it met its pre-registered bar, or a check after the fact confirmed it.
- "Directional" means the effect was real and pointed the right way, but too small or not profitable after cost.

## Summary

| Outcome | Studies | What they have in common |
|---|---:|---|
| **Passed** | 9 | Exits, gates and config checks. They cut losses; none is a new entry. |
| **Directional, not passed** | 11 | Real effects that are too small, could not be traded, or were not profitable after cost |
| **Failed** | ~62 | Almost every entry signal, indicator, level, setup and outside strategy |
| **Underpowered / stopped** | 10 | Not enough data to say yes or no (untested, not failed) |
| **Pending** | 5 | Forward tests still collecting |

**Bottom line:**
- No new **entry** has passed. Every pass is a way to lose less: better exits, gates that keep out worse names, and checks confirming the current setup.
- The desk's net loss per trade went from about −7.7 bp to about −3 to −4 bp in replays. The fix shipped 10/22 (the $20 floor) is expected to be worth about 0.6 bp more.

## 1. Passed (met the pre-registered bar)

| Date | Test | Result | What changed |
|---|---|---|---|
| 10/21 | Exit capture: do exits keep an injected edge? | **KEEPS AN EDGE**: 70-78% of a 3-min run kept | Showed exits are not the problem. The break-even entry edge is ~+11 bp. |
| 10/16 | Trail double-check exit (trail_dc), 8-day rerun | **+40.8 bp/day, t 2.29, both halves**; win rate 45% → 56% | Live since 10/16 |
| 10/14 | Ratchet idle leash 15 s | **+58 bp/day, t 2.1** vs 4 s | Shipped. A loss reducer: the book is still about −3 bp/trade. |
| 10/15 | Exit delay (trading-API rate-limit errors) | Stop gap cut from 3.4 s to 0.5 s with a 3 s tick | Engineering fix, live |
| 10/23 | Spread gate after costs | Refused names −12.8 bp vs board | Keeps the 0.05% cap |
| 10/1, 10/3 | Spread gate (earlier read) | Refused squares −0.93 / −0.81% vs −0.15% armed | Gate kept |
| 9/30-10/1 | Gap-down gate | Gap-down >1% names fade all day (z −3.7) | Shipped |
| 9/25-10/21 | Price floor ($10, then $20) | Under $10 = 28% of trades but 81% of the loss; exits keep 80-88% of an edge above $20 vs 32-54% under | Live; forward check running |
| 10/2 | Overnight config variants (17-18 variants, 10 years) | Live config best or tied; nothing beats it | No change (a confirmation) |

## 2. Directional: real and pointing the right way, but not passed

| Date | Test | What was real | Why it did not pass |
|---|---|---|---|
| 10/21 | Order-flow pilot (SIP trade flow) | **+1.5 to +1.9 bp, t ~3.5 in both halves**: a genuine signal | Far under the 5 bp bar; net −11 to −16 bp |
| 10/23 | Fibonacci golden pocket | +31 / +36 bp vs random entries (t 8-9) | Still nets −7.6 / −3.7 bp; it only loses less than random. Not Fibonacci-specific. |
| 10/12 | %R latch / RSI fire | Lift bar met in both halves; 15-min markout +3.8 / +11.2 bp (t 2.9 / 4.0) | Flat before the spread, so it loses the 20 bp spread |
| 10/15 | Right-tail runner | +17.6 bp vs the real fill (t 2.4) in half A | Runner net still negative; SIP arm t < 2 |
| 10/15 | News reaction | The headline move is real: +33 bp (t 19) | It happens within the headline minute and starts before publication; a 1-second SIP entry nets only +4 bp gross |
| 10/22 | Resting limit entries | Beat a market entry by ~+3 bp | Only a loss reducer; fills are adversely selected |
| 10/4 | Overnight S2 ranking, 2000-2015 | **Confirmed**: t 9.7, +8.6 bp gross | Hedged alpha unproven; paper lost −82 bp/night over 10 nights. Parked. |
| 10/1 | Month-end SPY flow (k=1) | +20.6 bp, t 3.7 over 289 months | k=1 was picked after seeing the data; the registered k=3 failed. First forward score 10/29-30. |
| 10/9 | Day-hold pilot | +17 bp/day vs random (t 1.5, 14 days) | The 10-year skeleton failed; forward run is −32 bp/day (t −2.2) at 9 sessions |
| 10/16 | Sideways filter (operator's slow-%R read) | Right direction when both halves are pooled (+11.7 vs −18.4 bp) | The halves reverse; kept arms −6.8 bp net |
| 10/14 | Board-name gates | $30 price was the one consistent lead | All four FAIL |

**Overturned on review** (these looked like passes):
- **Room below HOD (10/9).** The script printed PASS (+42 bp, t 4.6); the skeptic found an artifact.
- **Strength entry (+0.86%).** It used look-ahead.
- **Combined score (+6.6 bp on 8 days).** It failed on 60 earlier days.
- **Range-cap removal (+107 bp).** It compared different names.
- **Gap-down short (+47.6 bp).** It relied on look-ahead from the official open.
- **AGY's "+$294 if 5 minutes earlier".** That was hindsight.

## 3. Failed

**Entry signals and timing:**
- %R crosses, squares, pre-square, ignition, mid-rise, RSI-2 turn (10/13); oversold leave ▲ (10/15); pre-screen %R turns on 283k minutes (10/17)
- Connors RSI-2 green (10/14); RSI and MACD gap (60 days); confirm-then-buy (10/16)
- Limit below the signal (10/14); wait for a dip and buy if it holds (10/16); resting entry (10/3); flow-conditioned resting (10/22)
- Three-minute re-read: 17 of 18 rules (10/21); SIP volume breakout (10/3, held out); breakout confirmation (10/5)
- Zone entry and zone + square (10/2); squares after S/R levels; 7 S/R indicators (10/2); support arm (10/5); %R ≤ −60 gap arm (10/5)
- Order blocks / LuxAlgo, skip and range-trade (10/5); fair-value cheapness trigger (10/22); pairs reversion (10/19)
- Sweeps and dark-pool blocks (10/22); grading of the bar that just closed (10/19)
- Grok scalp recipe (RVOL/VWAP/EMA/RSI-7); Bollinger live callouts (9/27); Discord squeeze levels (9/27)
- Five pillars, RTH and premarket (9/27, 10,080 cells); off-book momentum (9/27); momentum runway (9/27)
- Strategy-edge rounds 1, 2, 4 and 5 (9/26: published intraday effects, volume/spread/pullback, ~430 cells)
- Halt reopens (10/3); market temperature (10/3); news-timed seats and sector lead-lag (10/15)
- Premarket bursts; quiet tape on delayed SIP; volume surges (direction 50/50)
- Early-entry arms (4 of 4, 10/13); 08:30 list as a seat (10/17)

**Confirmation indicators** (10/21, desk arms and 110-session history):
- ADX, OBV, A/D, the article's confluence, video setups S3/S5/S6/S7
- Name history / HTF uptrend / round numbers (10/9)

**Name selection:**
- Board-name gates: $30 / early runway / 50-day / $500M (10/14)
- Needle-study cells; steady runway "$50+ trending" (60 days)
- Buying the top: room / overbought-age / stall gates (871 fills)
- Name facts lift the win rate but not the mean

**Exits** (other than the passes above):
- 42 stop / target / trail / ladder shapes ("exits cannot move the mean")
- Let-it-run looser trails (10/7); wider stop (10/3); RSI-2 red exit (10/14)
- Stop + %R double-check, both lines and one line (10/12)
- Exit at resistance; size by room (10/5)
- Exit sweep arms cool30 / lob / init06 / seat150 / np_dc (10/16); np180 cut
- Holding longer (5/15/30 min worse)

**Short side:**
- Short the fade; short at squares (10/2); gap-down short (10/16, 10 years)

**Outside strategies:**
- Option selling / VRP (10/2); S&P index add/delete (10/2); monthly momentum (10/2)
- Late-day SPY/QQQ momentum (round 3); calendar flows TOM / QE / pre-holiday / tax / OPEX (10/16)
- Longer holds vs SPY, PEAD, gap-and-go (9/27); overnight RSP hedge (10/8); overnight momentum as a hedged edge

**Admission knobs** (10/14 replays):
- Loosening RVOL / RSI / tight-move all lost (−66 to −222 bp/day)
- The "momentum from $1" source
- AI hold-check shadow (no signal)

**Late failures:**
- Resistance overhead on desk fills (10/22, +0.1 bp)
- Floor-gate counterfactuals for the float cap (no evidence either way)

## 4. Underpowered or stopped (not enough data, so neither passed nor failed)

| Date | Test | Why |
|---|---|---|
| 10/23 | Short squeeze (FINRA days-to-cover) | Detectable effect ~102-119 bp vs a 50 bp bar |
| 10/22 | Level-2 walls | Only 42-58 comparisons; needs forward depth recording |
| 10/22 | Fair-value timing layer | Only 3% of entries were "cheap" |
| 10/22 | Closing-auction imbalance | Parked: Databento refused payment |
| 10/21 | Indicator H1 and S4 | Too few cases |
| 10/7 | Earnings drift | Detectable effect ~60 bp; halves disagree |
| 10/4 | Nasdaq book imbalance (depth) | Detectable effect ~7 bp > 5 bp bar; leaned negative |
| 10/16 | CONF2, TRER | Too few |
| 10/19 | Pairs (by the trade floor) | Negative out of sample anyway |
| 10/22 | Breakouts on desk fills | Too rare |

## 5. Pending (forward tests still collecting)

| Test | Read |
|---|---|
| Month-end SPY flow | First forward score 10/29-30 |
| price30 | ~10/27 |
| Support hold-to-close | ~11/2 |
| Day-hold forward | 40 scored sessions, ~11/13 |
| $20 floor + supply settings (floor20, restarts 10/26) | ≥ 20 sessions and 150 fills, ~early Dec |
