# Entry tests by category, and what is left (2026-10-23)

Built from `docs/TEST_SCORECARD_2026-10-23.md`. Exits, gates and outside strategies are left out. This page covers **entries only**: what makes the desk buy a name at a given minute.

## A. Categories already tested

| # | Category | What we tested | Verdict |
|---|---|---|---|
| 1 | **Oscillators / indicators** | %R crosses and squares, RSI, RSI-2 (Connors), MACD, ADX, OBV, A/D, Bollinger, confluence scores, pre-screen %R turns on 283k minutes | **FAILED**: equal to a random minute |
| 2 | **Breakouts** | Day-high breaks, SIP volume breakout (held-out), S/R breakout and confirmed breakout, opening-range breakout on top gainers (9/26, small sample) | **FAILED** |
| 3 | **Price levels** | Support/resistance (7 kinds), VWAP, premarket high, order blocks, round numbers, zones, Fibonacci | **FAILED**. Near support and deep pullbacks lose less, but still lose. |
| 4 | **Pullbacks / dips / mean reversion** | Limit below the signal, wait-then-buy, Fibonacci, oversold leave, pairs reversion, fair-value cheapness | **FAILED** |
| 5 | **Timing of the same signal** | Earlier entry (4 arms), confirm-then-buy, 3-minute re-read (18 rules) | **FAILED** |
| 6 | **Volume** | RVOL, volume pace, volume surges, quiet tape, five pillars (10,080 cells) | **FAILED**. Volume predicts movement, not direction. |
| 7 | **Order flow / microstructure** | Lee-Ready trade flow, book imbalance, ISO sweeps, dark-pool blocks, flow + resting bid, L2 walls | **Mostly FAILED.** Trade flow is real but small (+1.5-2 bp, t 3.5). Book imbalance and walls were underpowered. |
| 8 | **News / catalysts** | Fresh headlines (Alpaca/Benzinga), sector lead-lag, earnings drift | **FAILED** (news). The move happens within the headline minute. Earnings drift was underpowered. |
| 9 | **Name selection** | Price tiers, $30, float, 50-day, early runway, trendiness, room below HOD, 15-feature needle study | **FAILED as an edge.** The win rate goes up, the mean does not. Only price ≥ $10/$20 helps (loss cut). |
| 10 | **Market context** | Market temperature, gap-down, above-50-day + news, sideways filter | **FAILED** as an entry. Gap-down works as a gate. |
| 11 | **Outside signal services** | Bullish Bob Discord levels, Bollinger live callouts, Grok's scalp recipe, AGY early entries | **FAILED** |
| 12 | **Short side** | Short the fade, short squares, gap-down short | **FAILED** |
| 13 | **Forced buyers / flows (intraday)** | Halt reopens, short squeeze (FINRA days-to-cover), closing-auction imbalance | Halts FAILED. Short squeeze UNDERPOWERED. Closing auction PARKED (Databento would not take payment). |

**Pattern:** categories 1-12 all ask the same question in different ways: *will this name go up in the next few minutes?* Each one asks it from the name's own recent price, volume or tape. On these names that question looks like a coin flip, and the spread is what each flip costs.

## B. Categories not yet tested (from the research)

These are ranked by how good the evidence is and whether the desk can test them on free data it already has.

| Rank | Category / idea | Evidence | Fits the desk? | Free data? | Status |
|---|---|---|---|---|---|
| **1** | **End-of-day reversal**: buy the day's early **losers** for the last 30 minutes | Baltussen, Da & Soebhag (2025; 2nd place, Quantpedia Awards 2025). A stock's return early in the day predicts its last-half-hour return in the **opposite** direction. The effect is mostly in the losers: buying by retail traders and short sellers covering into the close. | Yes. Long-only fits (the gain is in losers bouncing), and the hold is short (~30 min). | Yes: warehouse SIP 1-minute bars, 120 sessions | **Untested.** Our late-day test was index momentum (SPY/QQQ), a different effect. |
| **2** | **Leveraged-ETF rebalancing into the close** | Barbon, Beckmeyer, Buraschi & Moerke (2022). Leveraged-ETF rebalancing pushes stocks and ETFs in the day's direction near the close. It explains ~1/3 of late-day volatility (Rodier et al. 2012). The effect reverses at the next open and has shrunk over time. | Partly: it trades ETFs (SPY/QQQ/sector ETFs), not the board | Yes: SIP bars | **Pre-registered 10/19 (`levetf_close_prereg.json`), never run** |
| **3** | **Stocks-in-play opening-range breakout** (as the paper does it) | Zarattini, Barbon & Aziz (2024, SSRN 4729284). Top 20 names by first-5-minute volume vs their 14-day average, taken from the 1,000 most liquid stocks (>$5, ATR >$0.50). Trade the 5-min range break, long or short. They report Sharpe 2.8; a QuantConnect recreation gets Sharpe 2.4. Backtest only, not peer-reviewed. | Yes: entries from 09:35. The paper also shorts. | Yes: daily and 1-min SIP bars; needs a fresh 1,000-name daily universe | **Only a rough version was tested** (9/26: top-10 gainers, small sample, signs flipped between periods). The paper's opening-volume ranking and ATR stop were never tested. |
| 4 | **Same half-hour yesterday** (intraday periodicity) | Heston, Korajczyk & Sadka (2010, *Journal of Finance*). A stock's return in a half-hour slot predicts its return in the same slot on later days, for up to 40 days. Strongest in the first and last half-hour. Holds in the UK and Brazil, weak in China. | Yes: it picks *when* to buy a name | Yes: the warehouse has 120 sessions | **Untested.** The effect is small (a few bp per half-hour), so it may not clear cost. |
| 5 | **Overnight vs intraday tug of war** | Lou, Polk & Skouras (2019, *Journal of Financial Economics*). Stocks keep their intraday pattern and their overnight pattern, and the two pull against each other. Many strategies earn only in one session. | Yes, as a name picker: names whose recent gains come intraday | Yes: daily bars | **Untested** as an intraday name picker. Our overnight work was the other half. |
| 6 | **Analyst upgrades** (first 20-30 min) | Bradley et al. (2014, *Journal of Finance*). +1.83% in the 30 minutes after an upgrade, using newswire timestamps; with delayed (I/B/E/S) timestamps the return is near zero, so speed decides everything. | Like news: the move is very fast | Partly: Benzinga headlines via Alpaca | **Partly tested** inside the news study (all headlines pooled). Same speed problem as news, so a low prior. |
| 7 | **Narrow range / inside bar (NR7)** | Practitioner only (Crabel). No peer-reviewed test on US stocks found. | Yes | Yes | Untested. Low prior; cheap to run. |
| 8 | **Retail order imbalance** (subpenny-priced trades) | Boehmer, Jones, Zhang & Zhang (*Journal of Finance*). Net retail buying predicts ~+10 bp over the **next week**. It does not pay where retail attention is high (Barber, Odean et al.). | Poor: the effect plays out over a week, not minutes | Yes: SIP trades | Untested. Wrong horizon for a scalp desk. |
| 9 | **Options-driven flows** (dealer gamma, unusual options activity) | Barbon & Buraschi, "Gamma Fragility" (working paper): gamma imbalance explains stock intraday momentum and reversal. Unusual options trades that are made public do not earn abnormal returns (Jiang & Strong). | Yes | **No**: needs options data (paid) | Untested. Blocked by "no spend until proven". |

## C. Recommendation
1. **End-of-day reversal first.** It is the strongest new evidence, long-only, testable on data we already have, and nothing like what we have tried. It also fits what we keep finding: these names fade, and the losers bounce at the close.
2. **Leveraged-ETF close second.** It is already pre-registered; it needs only the skeptic and a run.
3. **Stocks-in-play ORB third, as the paper defines it.** It is the closest published match to what the desk already does (in-play names from the open), and our 9/26 version was not the paper's.

Each one gets a prereg and a skeptic review before any data is read, as usual. Nothing changes on the desk, which stays frozen for the floor20 window.

## Sources
- Baltussen, Da, Soebhag, *End-of-Day Reversal*: https://www3.nd.edu/~zda/EOD.pdf ; https://www.eur.nl/en/news/end-day-reversal-pattern-second-place-quantpedia-awards-2025
- Barbon, Beckmeyer, Buraschi, Moerke, *Liquidity Provision to Leveraged ETFs and Equity Options Rebalancing Flows*: https://papers.ssrn.com/abstract=3925725
- Zarattini, Barbon, Aziz, ORB for stocks in play: https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- Heston, Korajczyk, Sadka, *Intraday Patterns in the Cross-section of Stock Returns*: https://ar5iv.arxiv.org/html/1005.3535
- Lou, Polk, Skouras, *A tug of war*: https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- Bradley, Clarke, Lee, Ornthanalai, analyst recommendations intraday: https://www.scheller.gatech.edu/directory/research/finance/lee/pdf/jump2-1-12.pdf
- Boehmer, Jones, Zhang, Zhang, *Tracking Retail Investor Activity*: https://www.finance.dauphine.fr/wp-content/uploads/2017/02/retail20170405.pdf
- Barbon, Buraschi, *Gamma Fragility*: https://abarbon.com/papers/gamma-fragility
- NR7 practitioner backtest: https://unofficed.com/courses/backtesting-buddha/lessons/backtesting-narrow-range-inside-bar-strategy-using-python/
