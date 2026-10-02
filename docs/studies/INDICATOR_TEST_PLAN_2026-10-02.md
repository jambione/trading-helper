# Indicator test plan: what is still worth testing (2026-10-02, weekend 10/2–10/4)

Source: the user's TradingView favorites list (screenshots, 10/2). The question each test answers is the one the user
used these tools for when trading by hand: **is this name worth opening right now, and how far can it run?**

## Already tested: do not redo

| Family | Where | Result |
|---|---|---|
| %R exhaustion crosses and squares | `wr-triggers-see-no-runway`, `SQUARE_*`, `SHORT_SQUARES_2026-10-02` | random-minute equivalent |
| RSI / RSI2 / MACD / MACD gap | `rsi-and-macd-gap-add-nothing` (60 days) | ≤ random |
| Scalp recipes (RVOL/VWAP/EMA pullback/RSI7) | Grok scalp study | −36.5 bp net |
| Stops, targets, trails, SuperTrend-type exits | `exits-cannot-move-the-mean` (42 shapes) | ~0 gross |
| Volume pace / RVOL / volume + above VWAP + above prior close (V1) | `STRATEGY_EDGE_ROUND4`, `volume-is-predictable-not-directional` | movement, not direction |
| Bollinger %B, bandwidth, 1m squeeze level as a ranking input | `BOLLINGER_STUDY_2026-09-25` | not proven |
| Multi-timeframe trend agreement (1/5/10m EMA20) | `edge2_mtf_prereg.json`, round 2 | no survivor |
| Daily breadth/regime filter | round 5 T6 | no help |
| Bullish Bob published squeeze levels L1/L2, break entries | `DISCORD_SQUEEZE_LEVELS_2026-09-27` | −2 to −3%/trade, worse than random |
| Premarket-high breakout and VWAP reclaim, **premarket small-cap movers** | `PREMARKET_FIVE_PILLARS_2026-09-27` (P2, P4) | no help |
| Room below the high of day | `BOLLINGER_STUDY` (+), `buying-the-top-is-not-the-loss` (−) | **conflicting** |

Not testable: Nadaraya-Watson (LuxAlgo), LuxAlgo/Flux order blocks, SMC, market-structure-break. They repaint, so no honest history exists.

## Untested and worth testing

All on the desk's own population: RTH arms and fills at **$10+** (the current floor), from the recorded arm/shadow rows and
fills since 9/23, plus same-name random minutes as the control. Features are computed from 1m SIP history bars using only
data available at the arm minute.

| # | Test | Feature at the arm minute | Why it is worth a slot |
|---|---|---|---|
| L1 | **Overhead resistance** | % distance to the nearest level above: prior-day high, premarket high, today's high, round dollar / half dollar | The user's main manual filter. Only today's high has been tested. |
| L2 | **Support below** | % distance to the nearest level below: VWAP, prior close, premarket low, prior-day high if already cleared | Room to a natural stop; reward-to-risk L1 ÷ L2 |
| L3 | **Broke and held vs pressing into** | Price above prior-day high / premarket high for ≥ N minutes (retest held) vs within 0.3% under it | Tested only premarket on small caps (P2), never RTH $10+ |
| L4 | **High-volume price zones** | Distance to the nearest volume-profile node (today's + prior day's 1m volume by price) above and below | What the SRchannel / high-volume-box tools draw; untested in any form |
| V1 | **VWAP position as a gate** | % above VWAP, minutes since the last VWAP reclaim | Runway study saw +46 vs +11 bp descriptively; never tested as a gate on arms |
| R1 | **Room below the high of day, replicated** | % below today's high | Resolve the conflict on post-$10-floor arms only |
| M1 | **Market tide** | SPY and QQQ 15m and 30m return at the arm; share of the day's admitted names up in the last 15 m (a free $TICK proxy) | Only a daily regime filter was tested |
| M2 | **Relative strength vs SPY, intraday** | Name return minus SPY return since the open and over the last 30 m | Daily RS exists (`rs_screener.py`); intraday never tested |
| L5 | **Ported open-source S/R indicators** (added 10/2 15:50 ET, before any result) | LuxAlgo "Support and Resistance Levels with Breaks": last confirmed 15/15 swing high/low and whether price is above, inside or below them. LonesomeTheBlue "Support Resistance Channels": 10/10 pivots in 290 bars, channel width 5% of the 300-bar range, strength = pivots×20 + touches, top 6; % to the nearest channel above/below, inside a channel | The user's TradingView S/R tools, ported from their published logic; pivots are used only once confirmed |
| S1 | **Squeeze release event** | First bar Bollinger(20,2) leaves Keltner(20,1.5) with LazyBear momentum > 0 and rising, 1m and 5m | Only the squeeze level was tested, never the release |

## Rules (fixed before any result)

- **Outcome:** net return to the live exit (replay of the live leash) and a fixed 15-minute hold, after the full quoted SIP spread.
  The control is random minutes in the same name and hour.
- **Cut:** terciles of each feature (plus the named cut-points in L3/S1), compared with the middle and with the control.
- **Split:** alternate days A/B for discovery. A cell that is positive in both halves then goes to the **60-day history**
  (`combined-score-positive-held-out` failed exactly there). Promote only if it holds there.
- **Pass bar:** better net than the control by ≥ 5 bp with day-clustered t ≥ 2 in both halves and on the 60 days, n ≥ 100.
  Name selection that lifts the win rate but not the mean does not pass (`name-selection-lifts-win-rate-not-mean`).
- **Multiple comparisons:** about 10 tests × 3 cuts × 2 outcomes ≈ 60 cells (L5 adds about 20 more). Expect 2–3 false positives at t 2. Only the 60-day check counts.
- **Log `n` and decision counts first**: a cell with 0 trades is not a verdict (`replay-verdicts-were-vacuous`).
- **Data:** free feeds only. Off-hours fetches on the mini, under the Alpaca rate limit; record 429 drops (`rvol-pace-gate-graded-2026-10-01`).

## Schedule

- **Fri night:** build the arm-time feature table (one script, one row per arm/fill and per control minute). Run L1, L2, L3, R1.
- **Sat:** L4, V1, M1, M2. Run any surviving A/B cells on the 60 days.
- **Sun:** S1, the 60-day re-checks, and a one-page verdict before Monday's open: which feature, if any, becomes a gate or a ranking input.
