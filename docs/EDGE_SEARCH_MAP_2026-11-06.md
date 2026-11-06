# Edge search map (operator 11/06: "I'm trying to find a real edge"; decision date ~12/1)

Built from docs/studies (about 120 study files), docs/running_list.md (105 rows) and memory. Cost assumed 4-5 bp round trip (measured 11/06). "FAIL" = pre-registered
and failed its own bar; "NULL" = no effect; "UNDERPOWERED" = could not be read. Nothing here is a recommendation to trade.

## Already searched (do not repeat without a new data source)
| Family | What was tested | Result |
|---|---|---|
| Entry timing on the desk's own signals | %R turns, empty square, pitch/trough/slope bands, uptrend pullback, trend dip, Fibonacci pullback, buy-stop through the turn, order-flow confirmation, resting entries, limit-below, 1-2-3 (11/06), early/late entries | all ~0 lift vs random minutes; net -5 to -16 bp |
| Entry filters on never-greens | 15+ filters (range position, run size, steep rise, sideways shape, quiet tape, RSI, re-entry, time cutoff, confirmation indicators, order flow) | FAIL or label-only; the 11/06 quiet-tape held-out also FAIL |
| Exits | 42 stop/target/trail/ladder shapes, never-green cuts, tight stops, triangle, leash, late breakeven, early-path cut (11/06) | gross ~0; cuts and tight stops lose 3-24 bp/trade |
| Breakouts / levels | ORB (16,577 trades, 8 yrs), premarket/prior-day high, VWAP reclaim, S/R levels, order blocks, volume-burst breakout, NR7/inside | FAIL (-7 to -45 bp) |
| Momentum / names | momentum alerts, movers, trending, Stocktwits, mention bursts, Discord squeeze levels, news and sector, catalyst pullback, earnings drift, halts | FAIL; premarket alert names fade ~3.7% in 30 min (11/06 look) |
| Market structure | order flow (OFI), sweeps/blocks, flow compression, L2 walls, book depth, spread gates | real but +1-2 bp, below cost; walls UNDERPOWERED |
| Multi-day / calendar | overnight momentum (mostly beta), monthly momentum, month-end/QE/OPEX/tax-Jan, index adds/deletes, short interest/squeeze, pairs, RSP hedge | FAIL or beta |
| Options / carry | variance risk premium | no alpha since 2007 |
| Shorting | gap-down short, short the fade, short squares | FAIL / coin flip |
| Sizing / risk | R sizing, brakes, slot counts | does not change the mean |
| Data quality | IEX vs SIP bars for the entry measures (11/06) | same AUC; SIP only fills gaps |

## Not yet searched (candidates, ranked by my honest prior; each needs its own prereg + skeptic + strict stats)
| # | Idea | Why it might be different | Data | Prior |
|---|---|---|---|---|
| 1 | **Premarket on momentum names, real arms, SIP** | only place with big moves; every other test was RTH | real-time SIP for a FULL month (dry run first) | 10-15% |
| 2 | **Real-time quiet-tape / spread gates on SIP** | worked only in real time in the 9/30 study; the 11/06 held-out used delayed-safe historical bars and failed, so this is a low prior now | SIP real-time | 5% |
| 3 | **Intraday relative strength vs SPY/QQQ at a fixed time (e.g. 10:00)** for liquid names, held to a fixed time | cross-sectional, not time-series; the desk only looks at the name itself | free historical SIP bars, ~400 names x 119 sessions | 8-10% |
| 4 | **Closing-auction / last-30-minute drift conditioned on the day's move** | structural flows, not a pattern in the name | free historical SIP + auction prints | 5-8% (EOD reversal and late-day index momentum already null) |
| 5 | **Leveraged/inverse ETF rebalancing flow into the close** | documented mechanical flow, strongest on big index days | SIP bars of the ETFs + underlying | 5-8% |
| 6 | **Spread compression at the arm** (queued, item 8) | direct tell of buyers stepping in | free historical SIP quotes | 10-15% |
| 7 | **Double arm: second arm = the open** | uses name selection where the signal is | desk replay | 8% |
| 8 | **%R lookback halved** (queued) | indicator speed | desk replay | 8% |
| 9 | **Cheap names / low float separately, with the real spread** | cheap names carried 81% of lifetime loss, but never tested net of real SIP spread on the exchange-quoted book | free historical SIP quotes | 3-5% |

## What the map says
1. The desk's own universe (liquid movers) has been searched from almost every direction at day-trading horizons; every family above came back at or below zero after cost.
2. The only untested territory with large moves is premarket and small names, and it needs real-time SIP and a full month.
3. The cross-sectional ideas (3-5) are the only cheap, free-data scans left; their priors are low.
4. An honest endpoint: by about 12/1, either one of the above survives a strict, held-out, both-halves test, or the desk runs at minimum cost with the monitoring built this month.
