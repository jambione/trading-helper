# Discord "Squeeze Potential Alert" levels: backtest (2026-09-27, read-only)

Script: `tools/studies/discord_squeeze_levels_study.py` (`fetch` / `analyze`). Inputs were the 191 alert posts
(ticker, time, L1/L2/L3 only) and 18 nightly watchlists from Bullish Bob's Trading Hub, 9/1 to 9/25/2026 (18 sessions).
The raw Discord text is not committed. Data: Alpaca historical **SIP** 1-min bars from 04:00 ET, SIP quotes for the spread at every entry,
and **IEX** 1-min bars from 06:30 to 09:30 for the feed check. 190 of 191 alerts have data; `FTFTF` (9/15) has no SIP bars.

## Bottom line
1. **Buying the break of the published levels loses money after realistic costs, and before costs it is at best flat** (zero-cost X1: E1 −1.3%, E2 −1.8%, E4 −0.4%, E3 +0.1%; zero-cost 30-min hold is negative for every entry). None of the
   12 entry×exit combinations has a positive average or median. Every level-based entry did **worse than a random minute on the same
   name after the alert** (the baseline). The least-bad cost-inclusive result is E1/E3 + X1, the $0.05/$0.08/trail bracket, at about
   **−2.3% to −2.5% per trade (−$23 to −$25 per $1k)**. Both halves are negative for every combo, so "robust across halves" only means
   robustly negative. With 18 days (9 per half) and 33 or fewer E1 trades, this check is weak. It can confirm "no edge" but could never prove one.
2. **The levels are not "at the price".** At alert time the price was above L1 in only **1 of 188** cases. The median price was **31.7% below L1**,
   and L1 sat a median **33% above the premarket high so far** (above the PMH in 169/188). L1 then traded before 10:30 in only 36/188 cases,
   a 1-min close over L1 happened 32 times, and a close over L2 happened 20 times. When the break does happen, the median 30-min MAE is about −11% vs MFE +5.6%. It is a fade, not a squeeze.
3. **The free IEX feed cannot see these names premarket.** IEX shows **zero** bars before 08:00 ET on all 190 name-days.
   From 06:30 to 09:30 the median name-day has 0 IEX bars vs 90 SIP bars and 0 IEX trades vs about 9k SIP trades. IEX volume is **0.6%** of SIP
   (median 0.00%, p90 0.69%). In the 30 minutes after an alert, 125/176 alerts had **no IEX print at all**.
   Anything the desk builds on these alerts needs SIP (paid/live) or a broker's live quotes, not free IEX.
4. **Watchlists**: next-day names move far more than a random $1–10 control. The median 9:30–11:00 MFE is +4.8% vs +1.3%, and the PM range is 14% vs 1%.
   But direction is negative: the median close vs open is −1.9%, and only 43% close above the open (control 40%). VOLUME is the only category with a
   positive median close/open (+1.9%, 54% green, n=80). The lists find volatility, not direction.

## Method (exact)
- **Context at alert:** last SIP 1-min close before the alert minute, the premarket high so far, the prior daily close, and the gap.
- **Entries** (after the alert, through 10:30 ET). E1 = the first 1-min close > L1 (bars starting at or after the alert minute), filled at the next bar open.
  E2 = the same for L2. E3 = a stop at $0.01 over the prior completed clock-aligned 5-min high, first trigger after the alert minute, filled at max(level, bar open).
  E4 = the open of the bar after the alert minute. BASE = 5 random bar opens after the alert per alert (seeded).
- **Costs:** fill = raw + ½ × median quoted SIP spread (NBBO in the 60 s from the entry minute; if the NBBO did not change in that minute,
  the standing quotes from the prior 5 min are used, 78 cases). Add **+$0.01** for names under $5. Exits pay the other half: limit targets fill
  only when the bid (trade − ½ spread) reaches them, and stops/time exits fill at price − ½ spread. So each round trip pays ≈ one full quoted spread plus 1¢.
  Median spread at entry: E1 0.66%, E3 0.99%, E4 1.07% (p90 4–5%).
- **Exits:** X1 = thirds at +$0.05 / +$0.08, with the last third trailing $0.10 from the high once +$0.08 prints and a $0.15 stop on all pieces.
  X2 = +2% / +3%, a 2% trail and a −5% stop. On both, the stop is checked first inside a bar and anything left is flattened after 60 min. X3 = a 30-min hold. X4 = MFE/MAE over 30 min.
  `_0` rows = the same with zero spread and zero slippage (the signal alone).

## Compact table (all alerts, after costs; per $1k = avg % × 10)
| entry | exit | n | win | avg | median | per $1k | zero-cost avg |
|---|---|---|---|---|---|---|---|
| E1 close>L1 | X1 bracket $ | 33 | 30% | −2.50% | −1.81% | −$25 | −1.28% |
| E1 | X2 bracket % | 33 | 33% | −2.46% | −5.03% | −$25 | |
| E1 | X3 30 min | 33 | 30% | −8.51% | −6.09% | −$85 | −7.56% |
| E2 close>L2 | X1 | 21 | 29% | −2.64% | −1.68% | −$26 | −1.75% |
| E2 | X2 | 21 | 38% | −1.95% | −5.03% | −$20 | |
| E2 | X3 | 21 | 19% | −10.76% | −8.89% | −$108 | −9.97% |
| E3 5-min high +1¢ | X1 | 184 | 34% | −2.29% | −2.49% | −$23 | +0.09% |
| E3 | X2 | 184 | 32% | −3.10% | −5.26% | −$31 | |
| E3 | X3 | 184 | 26% | −4.32% | −4.76% | −$43 | −2.02% |
| E4 at alert | X1 | 185 | 34% | −3.00% | −2.95% | −$30 | −0.35% |
| E4 | X2 | 185 | 32% | −2.99% | −5.29% | −$30 | |
| E4 | X3 | 185 | 19% | −5.49% | −5.61% | −$55 | −3.08% |
| BASE random min | X1 | 921 | 39% | −1.49% | −1.63% | −$15 | +0.54% |
| BASE | X3 | 921 | 28% | −1.54% | −2.23% | −$15 | +0.61% |

MFE/MAE over 30 min from the cost-inclusive fill (medians): E1 +5.6% / −11.5%, E2 +5.6% / −12.6%, E3 +3.5% / −6.7%, E4 +4.4% / −9.7%, BASE +2.0% / −4.6%.

**Halves (alternate days; A = 9 days, B = 9 days), avg per trade after costs:**
E1X1 −3.1% / −2.2%; E2X1 −2.6% / −2.7%; E3X1 −2.4% / −2.2%; E4X1 −2.7% / −3.4%; E1X3 −5.8% / −10.1%; E3X3 −5.3% / −3.3%.
Positive days out of 18 (summed per day): E3X1 4, E1X1 3, E3X2 0, E4X2 0, E4X3 0.

**Splits (X1, after costs).** No bucket with n ≥ 10 is positive.
- Price: E3 <$2 −1.7% (n=93), $2–5 −3.4% (58), $5–10 −2.5% (20), >$10 −1.4% (13). The >$10 names are least bad for every entry.
- Alert time: E1 before 7:30 −0.7% (median +0.2%, n=14), 7:30–8:30 −4.4% (11), after 8:30 −3.0% (8). Early alerts are least bad; n is tiny.
- Above L1 at alert: only 1 alert, so this split is not testable. Price is almost always well below L1.

## IEX feasibility (06:30–09:30 ET, 190 alert name-days)
| metric | IEX | SIP |
|---|---|---|
| name-days with any bar 06:30–09:30 | 90 / 190 | 190 / 190 |
| name-days with any bar before 08:00 | **0** | — |
| median 1-min bars | 0 | 90 |
| median trades | 0 | ~8,970 |
| median bars 08:00–09:30 | 0 | 68 |
| volume share (aggregate / median / p90) | 0.61% / 0.00% / 0.69% | 100% |
| alerts with zero IEX bars in the 30 min after the alert (pre-9:30) | 125 / 176 | |

## Watchlists (next session, SIP; unique name-days)
| group | n | PM high vs pc | PM range | gap | MFE 9:30–11 | MAE 9:30–11 | close/open | close/pc | % green |
|---|---|---|---|---|---|---|---|---|---|
| all | 351 | +7.4% | 14.2% | +0.6% | +4.8% | −6.2% | −1.9% | +0.3% | 43% |
| EARLYBIRD | 98 | +38.0% | 30.2% | +17.4% | +8.5% | −7.5% | −2.7% | +11.1% | 43% |
| VOLUME | 80 | +22.6% | 24.5% | +6.1% | +8.9% | −6.7% | +1.9% | +2.8% | 54% |
| RECENTNEWS | 80 | +15.6% | 16.7% | +2.2% | +6.2% | −6.5% | −1.8% | +5.7% | 42% |
| TRENDY | 97 | +2.3% | 14.6% | −1.5% | +5.7% | −7.1% | −3.5% | −3.3% | 38% |
| MICRO | 66 | +5.0% | 16.9% | −1.2% | +4.3% | −7.7% | −3.8% | −3.9% | 35% |
| SQUEEZE POTENTIAL | 52 | +7.2% | 15.4% | +0.5% | +5.5% | −4.7% | −0.4% | −0.9% | 46% |
| CONTINUATION | 45 | +7.3% | 9.6% | +1.0% | +5.5% | −5.3% | −0.5% | +0.7% | 47% |
| random $1–10 control* | 509 | +0.4% | 1.0% | +0.0% | +1.3% | −1.5% | −0.4% | −0.4% | 40% |

*Control: 30 random active common stocks per day (Alpaca asset list, `is_common`, not levered ETPs), prior close $1–10 and prior-day volume ≥ 100k. All values are medians.
The 9/25 watchlist (for 9/28) is not yet testable.

## Caveats
- 18 sessions and one room. E1/E2 have n = 33/21, so a real small edge could hide in the noise, but the zero-cost results are negative too, so costs are not the only problem.
- The bracket time cap (60 min), the X2 trail (2%) and the quote-window choice are our assumptions. Spread is sampled at entry only, and exits assume the same spread.
- Alerts land within the posted minute. Bars starting at the alert minute are allowed for the E1/E2 close test, which slightly favors the entries.
- The owner's reported 10–25% gains are consistent with the MFE distribution (about half of E1 breaks reach +5% within 30 min), but the MAE is bigger and hits first often enough that mechanical exits lose.
