# LONGER HOLDS: do slower edges survive costs? (WORK IN PROGRESS, 2026-09-27)

**Status: WIP handoff. Usage ran out mid-run. Every number below is PARTIAL and preliminary.** The news-flagged
PEAD and gap-and-go cells have not run yet because the news fetch was still going. Nothing here has been reviewed for a
final call. Read-only study: no config edits, no restarts, no pytest.

## Plain words (partial)
- **Nothing beats buy-and-hold SPY on a beta-adjusted basis after realistic costs out of sample.** Only one idea is still standing:
  overnight holding of 12-1 momentum names. It has a real, stable gross edge of about 16 bp per night (IS 15.5, OOS 16.1).
  It survives **only if both legs fill in the auctions** (MOC buy, MOO sell) at about 2 bp per side or less. Break-even is about 5 to 6 bp per side.
  With marketable orders near 15:55 and 09:35 (modelled quoted spreads) it loses badly. The desk has never measured auction fills.
- 12-1 monthly momentum on the liquid set (ADV at least $50M, top decile, about 90 names) returned **+17.5% CAGR OOS vs SPY +12.3%**, but at beta 1.4.
  Alpha is +3.3%/yr with t 0.3, which is not significant. With 10 to 20 names it is worse: MDD −50% or more, and no alpha. In the $5M and $20–100 universes it does not beat SPY.
- Weekly short-term reversal: costs of 20–40%/yr destroy it. Gap/PEAD drift and daily gap-and-go, using the gap/RVOL **proxy** without the news flag,
  lose net of costs versus SPY OOS in every cell (e.g. gap ≥ 5% & RVOL ≥ 2, 60-day hold: net−SPY −2.5% per trade, t −6.9).

## Method (done)
- Data: free Alpaca SIP daily bars, adjustment=all, 2016-01-04 → 2026-09-25, for 6,227 US common stocks
  (NYSE/NASDAQ/AMEX, `is_common`, ETPs/funds/SPACs/prefs/notes removed by name, ARCA/BATS excluded). The set **includes 820 inactive/delisted
  assets**, and 16 renamed duplicates were dropped. Survivorship caveat: Alpaca's inactive list only covers delistings from 2018 on
  (2018: 90, 2019: 217, … 2022: 142), so names delisted in 2016–17 are missing. There is no delisting return: a delisted position is held as cash at its last close.
- The raw price for the ≥$5 and $20–100 filters comes from monthly RAW bars (raw/adj factor per symbol-month). It is wrong only in the month of a split.
- Universe: price ≥ $5 and ADV20 ≥ $5M (median about 1,600–2,700 names/day), liquid ≥ $50M (about 900), band $20–100 (about 1,180).
- **Costs:** Abdi-Ranaldo and Corwin-Schultz were cross-checked against **real SIP NBBO quotes** (300 name-days, 2025–26, at 09:35, 12:30 and 15:55).
  Both fail at the name level: rank correlation with quoted spread is AR 0.03–0.05 and CS 0.34, and they overstate the mean by 2–3x. The primary model is therefore a
  quote-calibrated regression, log spread ~ log ADV + log price, fitted separately for 09:35 (open trades) and 15:55 (close trades), with a mean correction and a 1-tick floor.
  The model's in-sample rank correlation is 0.72. Median quoted spread: liquid 29/8/7 bp at 09:35/12:30/15:55; $5–50M 78/16/12 bp; band 43/12/9 bp.
  Commission is 0. The calibration uses 2025–26 quotes and is applied to all years (caveat).
- Validation: IS 2017–2021, OOS 2022–2026-09, plus by-year figures. Metrics: CAGR, Sharpe (rf = 0), MDD, SPY correlation, beta, alpha with t-stat.
  Per trade: gross, cost, net, win rate, and net−SPY over the same window with t-stat. Multiple-testing tally so far: **102 cells** (the news cells will add more).
  At that count a t of about 2 is what you would expect by chance.

## Partial numbers (OOS 2022–2026-09; SPY CAGR 12.3%, Sharpe 0.76)
| cell | OOS CAGR | Sharpe | beta | alpha/yr (t) | turnover / cost drag |
|---|---|---|---|---|---|
| EW universe $5M (benchmark) | 4.9% | 0.33 | 1.07 | −7.1% (−1.6) | 95%/yr, 0.2%/yr |
| Momentum liquid, top decile | 17.5% | 0.65 | 1.40 | +3.3% (0.3) | 385%/yr, 0.4%/yr |
| Momentum liquid, top 20 | 12.6% | 0.49 | 1.68 | +1.4% (0.1) | MDD −52% |
| Momentum $5M, top 20 | −7.8% | 0.06 | 1.55 | −17.6% (−1.0) | |
| Momentum band, top decile | 10.7% | 0.50 | 1.24 | −2.1% (−0.2) | |
| Reversal liquid decile (1-week) | −10.4% | −0.16 | 1.50 | −25% (−2.6) | 4000%/yr, 20%/yr |
| Overnight, EW liquid, gross | 7.1% | 0.63 | 0.36 | +2.8% (0.6) | 3.0 bp/night |
| Overnight, EW liquid, 2 bp/side | −3.2% | −0.21 | | | |
| **Overnight top-20 12-1 momentum (liquid), gross** | 44.2% | 1.44 | 0.60 | +32.7% (2.7) | 16 bp/night |
| same, 2 bp/side (auction) | 30.4% | 1.08 | 0.60 | +22.7% (1.9) | ~500 round trips/yr |
| same, 5 bp/side | 12.1% | 0.55 | | +7.5% (0.6) | |
| same, modelled quoted spreads (15:55 in, 09:35 out, ~27 bp round trip) | −25.2% | −0.89 | | | |
| Top-20 day gainers overnight (liquid), 2 bp/side | 7.7% | 0.40 | | +5.0% (0.4) | IS 77% then decayed |

Overnight momentum top-20 at 2 bp/side, by year (strategy vs SPY): 2017 +41 (+22), 2018 +4 (−5), 2019 +26 (+31), 2020 +58 (+18), 2021 +25 (+29),
2022 +3 (−18), 2023 +8 (+26), 2024 +90 (+25), 2025 +21 (+18), 2026 +36 (+14). Dropping the best 1% of nights still leaves 10 bp/night.
The top-10/top-50, $20–100 band and $5M variants all show a gross edge of 13–16 bp/night. The raw log is `LONGER_HOLDS_partial_raw.txt` in /tmp/lh.

## What it would mean for the desk (if the auction-fill assumption holds)
The desk would stop flattening at 15:50 for these names. It would send MOC buys for about 20 top 12-1-momentum liquid names at the close
and MOO sells at the next open, every session. That means overnight gap risk on 100% of the book, earnings nights included (not yet excluded),
and a strategy that is flat intraday. It is a daily, mechanical rebalance with no intraday signals. **Recommendation so far:** do not change the live desk yet.
First run a small paper and live-size pilot that records auction fill vs. official open/close. Alpaca paper fills do not prove auction slippage.
Also add an earnings-night filter test. Otherwise the default answer is SPY.

## Remaining work
1. Finish the news fetch (`lh_news.py` was at day 200/2651 at 11:04 ET; it is resumable and niced), then rerun `lh_run.py analyze` to get the
   news-flagged PEAD (earnings) and gap-and-go (news) cells.
2. Test overnight momentum excluding earnings nights. Look at the MOO/MOC fill realism (the SIP official open vs the first NBBO mid).
3. Final doc and plain-words verdict. Add the pointers in docs/NAME_FINDING_DIRECTIVE_2026-09-27.md and the HANDOFF.md START HERE block (not done yet).

## Caches and resume (Mac mini, /tmp/lh)
`bars_all.pkl` (daily adj), `bars_rawM.pkl` (monthly raw), `panel.npz`, `universe_assets.json`, `assets_raw.json`, `quotes.json`,
`quote_sample.json`, `news_cands.json`, `news.json` (partial), and the modules `lh_*.py` (bundled in tools/studies/longer_holds_study.py; `unpack` writes them).
Resume:
```
cd /tmp/lh && export PYTHONPATH=/tmp/lh:/tmp:$HOME/repo/trading-helper:$HOME/repo/trading-helper/tools
pgrep -f lh_news.py || nohup nice -n 15 ~/repo/trading-helper/.venv/bin/python -u lh_news.py >> news.log 2>&1 < /dev/null &
# when news.log says "done":
nice -n 15 ~/repo/trading-helper/.venv/bin/python -u lh_run.py analyze > analyze_final.log 2>&1
```
Note: the Alpaca key is shared and rate-limited (another study plus the desk), so fetches run at about 4–8 requests/min for us.
Long jobs started from the Cursor shell die. Start them from the MacBook with `ssh mac-mini-away 'nohup … & disown'`.
