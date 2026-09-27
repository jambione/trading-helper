# LONGER HOLDS: do slower edges survive costs? (2026-09-27)

**Question.** Weeks of intraday tests found no free-data edge after costs. Do slower, documented edges — 12-1 momentum, short-term reversal, post-earnings / gap drift, overnight holding, daily gap-and-go — survive realistic costs on years of free Alpaca SIP daily bars?

**Answer.** **Nothing beats buy-and-hold SPY on a beta-adjusted basis after realistic costs out of sample**, except one idea that still needs a fill proof:
- **Overnight holding of top-20 12-1 momentum names (liquid, ADV ≥ $50M)** has a real, stable gross edge of about **16 bp per night** (IS 15.5, OOS 16.1). It survives **only if both legs fill in the auctions** (MOC buy, MOO sell) at about **2 bp per side or less**. Break-even is about 5–6 bp per side. With marketable orders near 15:55 / 09:35 (modelled quoted spreads, ~27 bp round trip) it loses badly (OOS CAGR −25%).
- Excluding earnings-flagged names from that overnight book (OOS 2022–2026, Benzinga headline screen) cuts the gross edge from 16.1 → **14.3 bp/night** and 2 bp/side alpha t from 1.9 → 1.5. Earnings nights are not the whole story; the edge is mostly elsewhere.
- 12-1 *monthly* momentum on the liquid set beats SPY on CAGR but not on alpha (OOS +17.5% vs SPY +12.3%, beta 1.4, alpha +3.3%/yr t 0.3).
- Weekly short-term reversal is destroyed by turnover costs (~20–40%/yr).
- News-flagged PEAD and gap-and-go: every OOS cell loses to SPY on net−SPY (typical t −2 to −9). Earnings-flagged gaps look less bad than unfiltered gaps on raw net, and still fail the SPY-relative test.

**Desk recommendation:** do **not** change the live scalp desk. First run a small paper and live-size pilot that records auction fill vs official open/close. Alpaca paper fills do not prove auction slippage. Default answer without that proof is SPY.

Script: `tools/studies/longer_holds_study.py` (unpack → `lh_*.py` under `/tmp/lh`). Helpers: `tools/studies/lh_earn_overnight.py`, `tools/studies/lh_auction_probe.py`. Raw log: `/tmp/lh/LONGER_HOLDS_raw.txt` on the mini.

## Method
- **Data.** Free Alpaca SIP daily bars, adjustment=all, 2016-01-04 → 2026-09-25, 6,227 US common stocks (NYSE/NASDAQ/AMEX; ETPs/funds/SPACs/prefs removed; ARCA/BATS excluded). Includes 820 inactive/delisted assets from 2018 on; 2016–17 delistings are missing. No delisting return (held as cash at last close).
- **Raw price** for ≥$5 and $20–100 filters from monthly RAW bars (wrong only in the month of a split).
- **Universes.** price ≥ $5 & ADV20 ≥ $5M (~1,600–2,700 names/day); liquid ≥ $50M (~900); band $20–100 (~1,180).
- **Costs.** Abdi-Ranaldo and Corwin-Schultz fail vs real SIP NBBO (rank corr AR 0.03–0.05, CS 0.34; overstate mean 2–3×). Primary model: quote-calibrated regression `log spread ~ log ADV + log price`, separate fits for 09:35 and 15:55, mean correction, 1-tick floor. In-sample rank corr 0.72. Median quoted spread: liquid 29/8/7 bp at 09:35/12:30/15:55. Commission 0. Calibration uses 2025–26 quotes applied to all years (caveat).
- **Validation.** IS 2017–2021, OOS 2022–2026-09. Metrics: CAGR, Sharpe (rf=0), MDD, SPY corr/beta/alpha(t). Per trade: gross, cost, net, win, net−SPY(t).
- **Multiple testing.** 102 cells evaluated (benchmark 3, momentum 9, reversal 6, overnight 18, pead 42, gapgo 24). At that count a t ≈ 2 is what chance can produce.
- **News.** Alpaca/Benzinga for 2,651 candidate event days (gap≥5% or day-up≥10% on RVOL≥2). Earnings flag: headline match and ≤3 symbols tagged.
- **Overnight earnings filter.** For each OOS session, top-20 12-1 liquid names; news window close 16:00 → next 09:35; drop names with an earnings-flagged article. 1,186 nights; 471 nights had ≥1 earn hit (739 name-hits).
- **Auction probe.** 80 liquid name-days in 2025–26: official daily open/close vs first/last SIP NBBO mid near 09:30 / 15:59.

## Compact results (OOS 2022–2026-09; SPY CAGR 12.3%, Sharpe 0.76)

| cell | OOS CAGR | Sharpe | beta | alpha/yr (t) | note |
|---|---|---|---|---|---|
| EW universe $5M (benchmark) | 4.9% | 0.33 | 1.07 | −7.1% (−1.6) | |
| Momentum liquid, top decile (monthly) | 17.5% | 0.65 | 1.40 | +3.3% (0.3) | no significant alpha |
| Momentum liquid, top 20 | 12.6% | 0.49 | 1.68 | +1.4% (0.1) | MDD −52% |
| Reversal liquid decile (1-week) | −10.4% | −0.16 | 1.50 | −25% (−2.6) | ~20%/yr cost drag |
| **Overnight top-20 12-1 (liquid), gross** | **44.2%** | **1.44** | **0.60** | **+32.7% (2.7)** | **16.1 bp/night OOS** |
| same, 2 bp/side (auction) | 30.4% | 1.08 | 0.60 | +22.7% (1.9) | ~500 RT/yr |
| same, 5 bp/side | 12.1% | 0.55 | | +7.5% (0.6) | ~break-even vs SPY |
| same, modelled quoted spreads (~27 bp RT) | −25.2% | −0.89 | 0.60 | −33% (−2.7) | |
| same OOS, drop earn-flagged names, 2 bp/side | 24.7% | | | +17.9% (1.5) | 14.3 bp/night gross |
| Top-20 day gainers overnight (liquid), 2 bp/side | 7.7% | 0.40 | | +5.0% (0.4) | IS strong, OOS decayed |

Overnight top-20 @ 2 bp/side by year (strategy vs SPY %): 2017 +41(+22), 2018 +4(−5), 2019 +26(+31), 2020 +58(+18), 2021 +25(+29), 2022 +3(−18), 2023 +8(+26), 2024 +90(+25), 2025 +21(+18), 2026 +36(+14). Dropping the best 1% of nights still leaves 10 bp/night; 58.9% of nights are green.

### News-flagged PEAD / gap (OOS net−SPY)
Every cell below is **negative** net−SPY out of sample. Earnings-flagged gaps are less bad than unfiltered gaps on raw net, and still lose the relative test.

| cell | OOS net | OOS net−SPY (t) |
|---|---|---|
| PEAD gap≥5% earn, buy close, hold 5d | +0.13% | −0.40% (−3.5) |
| PEAD gap≥5% earn, buy close, hold 20d | +1.20% | −0.07% (−0.4) |
| PEAD gap≥5% earn, buy close, hold 60d | +3.36% | −0.58% (−1.6) |
| PEAD gap≥5% any, buy close, hold 5d | −0.20% | −0.68% (−4.3) |
| PEAD gap≥5% any, buy close, hold 60d | +1.29% | −2.54% (−6.9) |
| PEAD gap≥10% earn, buy close, hold 20d | +0.99% | −0.15% (−0.5) |
| Gap-and-go ≥10% RVOL≥3 news, hold 1d | −0.15% | −0.28% (−1.9) |
| Gap-and-go ≥10% RVOL≥3 news, hold 20d | −0.63% | −1.75% (−4.8) |
| Gap-and-go $20–100 any, hold 20d | −0.26% | −1.39% (−3.1) |

Open entries pay ~3–4× the close-entry cost model and are worse on every PEAD cell.

## Auction fill realism (probe, not proof)
On 80 liquid 2025–26 name-days, the first SIP NBBO mid after 09:30 sat a median **+83 bp** above the official open print (p90 abs ~474 bp). Near 15:59 the last mid sat a median **+38 bp** above the official close (p90 abs ~489 bp).

That gap is the continuous book versus the auction print. It is **not** measured MOC/MOO slippage. It does say that treating daily open/close as a 2 bp fill is an assumption, not a measurement. True auction slippage needs broker fill logs against the official print.

## What it would mean for the desk (if auction fills hold)
Stop flattening these names at 15:50. Send MOC buys for ~20 top 12-1 momentum liquid names at the close and MOO sells at the next open, every session. Overnight gap risk on 100% of that book; flat intraday; mechanical daily rebalance; no intraday signals. Earnings-night filtering is optional and only mildly changes the edge.

**Do not ship that** until a fill pilot records live MOC/MOO vs official open/close at the size you would actually trade.

## Caveats
- Survivorship: Alpaca inactive list starts 2018; earlier delistings missing.
- Quote-cost model fitted on 2025–26 and applied historically.
- News/earnings flags are Benzinga-via-Alpaca headlines, not a full earnings calendar.
- Overnight earn filter covers OOS only (2022+); IS not re-scored with the filter.
- 102 correlated cells; treat t ≲ 2 as weak.

## Caches (Mac mini `/tmp/lh`)
`bars_all.pkl`, `bars_rawM.pkl`, `panel.npz`, `universe_assets.json`, `quotes.json`, `news_cands.json`, `news.json` (2,651 days), `earn_nights.json`, `on_sels.json`, `auction_probe.json`, `LONGER_HOLDS_raw.txt`, `results.json`.
