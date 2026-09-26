# Strategy edge study, round 2 (2026-09-26): liquid names only

Read-only study: no config change, no restart, no commit, no push, and no paid data. Scripts are `tools/studies/edge2_*.py` (uncommitted). The raw console output is in `docs/studies/STRATEGY_EDGE_ROUND2_2026-09-26_raw.txt`.

## Bottom line

- **Nothing passes cleanly.**
  - **Short-term reversal:** dead. All 72 cells are net negative on the tune period, and they do no better than random.
  - **Multi-timeframe trend alignment (test 6):** dead. **Looking at 1/5/10-minute trends together adds no edge on our data.** Its entries are no better than random entries in the same name and hour, and no better than the 10-minute trend alone.
  - **Overnight holding:** a positive point estimate for SPY/QQQ, but noisy, and the desk flattens at the close anyway.
- **One weak survivor: Gao et al. last-half-hour momentum on the index ETFs (SPY/QQQ/IWM/DIA).**
  - The rule: on a high-volatility day, when the return from prior close to 10:00 and the 15:00→15:30 return point the same way, trade that direction from 15:30 to 15:50.
  - It was the only cell that met the pre-registered promotion rule and still had enough holdout trades to score. It stayed positive on the untouched holdout.
  - It is small, rare and not significant on its own: about 0.5 trades a day, holdout t +0.8.

## Setup (pre-registered before any results)

- **Universe U2:** the round-1 LIQ list (100 large caps and ETFs) plus the sector ETFs not already in it (XLV XLY XLP XLI XLU XLB XLRE XLC). SPY, QQQ, IWM, DIA, XLK, XLF and XLE were already in the list.
  - A stock is eligible on a day if its **prior-day median SIP spread is ≤ 3 bp**; the 15 index/sector ETFs are always eligible.
  - About 43 names a day qualify (range 33–56).
- **Data:** free IEX 1-minute bars, 09:25–16:05, for **250 trading days**, 2025-09-29..2026-09-25. Cached in `ai_reports/edge_iex_bars/liq/`.
- **Split:** written to `ai_reports/edge2_split.json` at 12:32 ET, before any analysis.

  | Part | Dates | Days |
  |---|---|---|
  | tune | 2025-09-29 .. 2026-05-04 | 150 |
  | validate | 2026-05-05 .. 2026-07-16 | 50 |
  | holdout | 2026-07-17 .. 2026-09-25 | 50, touched once |

- **Promotion rule:** a cell goes to the holdout only if net > 0 on tune AND on validate, and the day-clustered t on tune+validate pooled is ≥ 2.0. At most 3 cells go, chosen by t. Benchmark cells cannot be promoted.
  - Test 6 was registered separately in `ai_reports/edge2_mtf_prereg.json` (a copy is in `docs/studies/`). It adds one requirement: the cell must also beat random entries on both tune and validate.
- **Cost:** the round-1 spread model, re-fitted on U2 samples only. It is the historical SIP quoted spread (free once 15 minutes old), median over 2 seconds.
  - New samples: 09:31, 13:30 and 15:50 on every 3rd day, plus every day of the last 20. Unsampled days use the name's median.
  - Time-of-day profile: 09:30–10:00 ×2.5, 10:00–10:30 ×1.5, then 1.0, 15:00–15:30 ×0.86, 15:30+ ×0.75.
  - The **full spread is charged once per round trip** (half in, half out). A SPY hedge leg adds SPY's spread. Typical cost is about 2 bp; for the index ETFs it is under 1 bp.
- **Statistics:** mean net per trade. The t is **clustered by day** and the 95% CI is a bootstrap over days.
  - Caveat: the pooled mean and the day-clustered t can disagree in sign. That happens when a few high-volume days carry many trades.

## Results: net bp per trade, tune | validate | holdout

| # | Idea (best/representative cells) | Tune net (t) | Validate net (t) | Holdout net (t) | vs random / benchmark (T \| V) | Trades/day | Pass? |
|---|---|---|---|---|---|---|---|
| 2 | Reversal, decile, 15m lookback, hold 30m, long/short raw | −2.9 (−9.5) | −3.3 (−6.2) | – | −0.7 \| −1.2 vs random | 219 | ✗ |
| 2 | Reversal, decile, 30m lookback, 60m hold, long only, SPY-hedged | −3.6 (−5.4) | −2.4 (−1.7) | – | −1.1 \| −0.2 | 95 | ✗ |
| 2 | Reversal, z ≤ −2 vs own volatility, 15m lookback, 60m hold, long only, hedged (best validate) | −3.9 (−1.7) | −1.1 (−0.1) | – | −1.7 \| +0.9 | 24 | ✗ |
| 2 | Reversal, all 72 cells | −1.5 to −15 | −1.1 to −17 | – | mostly < 0 | 18–219 | ✗ (gross ≈ 0 or negative everywhere) |
| 3 | Gao on SPY: prior close→10:00 sign, trade 15:30→16:00 | −2.5 (−1.7) | +5.8 (+3.0) | – | −1.6 \| +4.8 vs always long | 1 | ✗ (tune < 0) |
| 3 | Gao on SPY, trade 15:30→15:50 | +0.4 (+0.4) | +2.4 (+1.9) | – | +0.8 \| +1.7 | 1 | ✗ (pooled t < 2) |
| 3 | Gao on the 4 index ETFs, prior close→10:00, →16:00 | −2.8 (−2.2) | +5.1 (+2.6) | – | −2.1 \| +3.0 | 3.9 | ✗ |
| 3 | Gao on all 15 ETFs, prior close→10:00, →16:00 | −2.1 (−2.5) | +2.3 (+1.7) | – | +0.7 \| +3.2 | 14.6 | ✗ |
| 3 | Gao, 09:30→10:00 predictor (any group or condition) | −0.9 to −6.6 | −0.1 to −8.2 | – | mostly < 0 | – | ✗ |
| 3 | **Gao on the 4 index ETFs: signs of prior close→10:00 and 15:00→15:30 agree, high-vol day, 15:30→15:50** | **+4.3 (+1.8)** | **+5.9 (+1.0)** | **+9.0 (+0.8), n 24, CI [−4.7, +26.4]** | +2.3 \| −2.0 \| holdout +16.4 | 0.5–0.6 | **promoted; holdout positive but not significant** |
| 3 | Gao on QQQ, same rule, high-vol day, →16:00 | +6.7 (+1.3) | +11.1 (+1.6) | fewer than 10 trades (can't score) | +10.3 \| −2.1 | 0.1 | promoted, inconclusive |
| 4 | Overnight SPY, buy the close, sell the open (info only) | +4.3 (+0.9) | +8.0 (+1.1) | – | – | 1 | ✗ (t ≈ 1) |
| 4 | Overnight QQQ | +7.2 (+1.1) | +11.6 (+0.8) | – | – | 1 | ✗ |
| 4 | Overnight, all 15 ETFs / eligible stocks | −9.5 / −1.5 | +8.0 / +4.8 | – | – | 15 / 29 | ✗ (IEX opening prints are noisy) |
| 4 | Intraday, open→close: SPY / ETFs / stocks | +0.4 / +0.3 / −5.1 | +0.4 / −3.8 / −4.8 | – | – | – | ✗ |
| 6 | MTF S1 long (all 3 timeframes up, trend a), holds 15–120m or 5m-trend exit | −1.9 to −2.3 | −1.0 to −2.1 | – | name-hour random: 0.0 \| +0.3 to +1.6 · vs S3: −0.6 to −1.7 \| 0 to −2.8 | 160–230 | ✗ |
| 6 | MTF S1 long, trend b (higher highs and higher lows) | −1.7 to −2.6 | +1.3 to −1.9 | – | −0.5 to +0.5 \| 0 to +3.3 · vs S3 −0.7 to −1.8 \| +0.1 to +0.9 | 38–52 | ✗ |
| 6 | MTF S2 long (buy the 1m dip inside a 5m+10m uptrend), trend a | −1.9 to −2.1 | −1.2 to −2.2 | – | ≈0 \| −0.2 to +1.6 · vs S3 −0.8 to −2.0 \| −0.3 to −4.1 | 140–194 | ✗ |
| 6 | MTF S2 long, trend b | −3.0 to 0.0 | −1.9 to +4.9 (at 120m) | – | −1.1 to +2.0 \| 0 to +6.7 · vs S3 −0.6 to +1.2 \| +0.1 to +4.5 (t ≤ 1.7) | 9–12 | ✗ (tune ≤ 0) |
| 6 | MTF S3 control (10m trend turns up), trends a and b | −1.8 to −3.1 | −2.5 to +0.6 | – | ≈0 \| ≈0 | 44–150 | ✗ |
| 6 | MTF S1/S2 short mirror (info only) | −1.2 to −3.4 (one cell +2.3) | −3.2 to +0.9 | – | ≈0 | 9–211 | ✗ |

About 280 cells were tested in total (158 in the main script, 60 in test 6, plus variants). With that many cells, a couple of "promotable" ones would be expected by chance alone. That is why the holdout matters and why the one survivor is weak evidence.

## Detail and interpretation

### 2. Short-term reversal
- Gross is about 0 or negative in every cell: −1.5 to +0.6 bp on tune, −7 to +1.5 on validate.
- Against all eligible names at the same time and hold, the fades are 0.5–2 bp *worse*, and worst on the short side (fading winners).
- The hedge changes little.
- Round 1's hint that "a %R up-cross underperforms random" does not turn into a tradable fade in liquid names.

### 3. Gao et al. intraday momentum
- The 09:30→10:00 predictor has no value. Only prior close→10:00, which includes the overnight gap, carries any signal.
- It is positive on validate (for example SPY →16:00: +5.8, t +3.0) but negative on the tune period (−2.5). This is regime-dependent, not stable.
- Requiring the 15:00→15:30 return to agree in sign, on high-volatility days (first-30-minute realized volatility in the top third of the ETF's own trailing 20 days), is the one version that was positive in all three periods on the index ETFs.
  - 15:30→15:50: tune +4.3 bp, validate +5.9, holdout +9.0.
  - Only about 0.5–0.6 trades a day, and t never reaches 2 in any single period.
  - Against simply holding long over the same window it wins on tune and holdout and loses on validate.
- Exiting at 15:50 (the desk's flatten time) works as well as or better than 16:00 in the conditioned version. The 16:00 bar is also the one IEX handles worst: there is no closing auction on IEX.

### 4. Overnight vs intraday (information only)
- SPY and QQQ overnight holds are positive in both parts: +4 to +12 bp net at half the spread each end, but t ≈ 1. This is consistent with the published overnight drift.
- Intraday (open→close) is flat for SPY and negative for stocks.
- The desk flattens at the close, so this is not actionable without a mandate change.
- IEX opening prints are noisy, which inflates the variance, especially for the sector ETFs.

### 6. Multi-timeframe trend alignment (Jonathan's idea)
- **Gross is 0 to +1 bp for every signal, hold and exit**, so net is about −2 bp, i.e. minus the spread.
- **Against the name's own random mean for that clock hour** (same name, same hour, all days of the same period): S1, S2 and S3 are within ±0.5 bp on tune and 0 to +2 bp on validate (t ≤ 1.8). No edge.
- **Against S3** (the 10-minute trend alone):
  - S1 and S2 with trend a are *worse* by 0.6–2 bp on tune (t −1.7 to −3.2).
  - S2 with trend b is +0.3 to +1.2 on tune and up to +4.5 on validate at 120 min (t ≤ 1.7), with only 9–12 signals a day.
  - So the lower timeframes add nothing reliable.
- **Answer: no. Looking at multiple timeframes does not add an edge on our data.** It changes which minutes you trade, not what they earn.
- **Two baselines, one of them biased:**
  - The pre-registered baseline (random entries in the same name, same day, same hour) makes trend signals look 2–16 bp worse than random. That is a look-ahead artifact: random entries earlier in the same hour catch the very move that later creates the trend signal.
  - So I added the name-hour, all-days baseline after seeing this, and flag it as not pre-registered. Neither baseline shows an edge.

## Recommendation: the single best candidate and what a paper trial looks like

**Candidate: "late-day index momentum, conditioned."**
- **When it trades:** on days when first-30-minute realized volatility in SPY/QQQ/IWM/DIA is in the top third of that ETF's own trailing 20 days.
- **Signal:** at 15:30, if the return from prior close to 10:00 and the 15:00→15:30 return have the same sign, trade that direction.
- **Exit:** at 15:50.
- **Evidence:** +4.3 / +5.9 / +9.0 bp net (tune / validate / holdout), about 0.5 trades a day, t < 2 in every part.
- **Plainly, this is not proven.** It is the only thing in two rounds that survived an untouched holdout, and the economics are tiny: +5 bp on $1,000 is $0.50 a trade.

**Paper trial, as an observe-only sidecar with no change to the live desk:**
- Log the signal daily at 15:30 for SPY, QQQ, IWM and DIA.
- Place paper orders only if you approve: a marketable limit at the ask/bid ±1 cent at 15:30:05, sized at a fixed notional (e.g. $5k per ETF). Close with a marketable order at 15:50.
- Record the SIP mid at both ends (the nightly exec report already does this) so fills are scored against the true mid, not IEX.
- Run it for 60 trading days, which should give about 30–35 trades.
- **Success:** mean net > 0 with a CI excluding −5 bp, and fill cost ≤ 1 bp per side.
- **Stop early:** if cumulative net after 20 trades is below −20 bp per trade.
- **In parallel, for free:** extend history to 2–3 years of IEX data for these 4 ETFs only, and re-test this exact specification with no retuning. That is the cheapest way to see whether it is real.

**Everything else from rounds 1–2 should be considered closed.** That covers reversal, MTF alignment, %R, ORB, gaps, and first-30-minute momentum.
- The desk's structural problem is still roughly zero gross against a 2 bp (liquid) to 15–20 bp (current universe) spread.
- Until a signal clears about 3 bp gross in liquid names, the profitable move is to trade less, not differently.

## Caveats
- **Data:** IEX 1-minute prints, not SIP bars. There is no closing or opening auction, so 16:00 and 09:30 prices are the IEX prints nearest those times.
- **Spread:** the model uses samples on every 3rd day for older data.
- **Universe:** today's large caps (mild survivorship bias).
- **Sample size:** 250 days in total, and the per-day effects have only 50 holdout observations. The QQQ-only cell had fewer than 10 holdout trades.
- **Multiple testing:** about 280 cells. The holdout was run once, only for the two promoted cells.

## Files
- `tools/studies/edge2_fetch.py`: trading days, pre-registered split, IEX bars, spread samples
- `tools/studies/edge2_study.py`: tests 2, 3, 4 (`holdout` argument = the one-time holdout run)
- `tools/studies/edge2_mtf.py`: test 6 (spec in `docs/studies/edge2_mtf_prereg.json`)
- `tools/studies/edge_common.py`: shared loaders and spread model (gained an optional name filter)
- Caches on the mini: `ai_reports/edge_iex_bars/liq/`, `ai_reports/edge_spreads.json`, `ai_reports/edge2_split.json`, `ai_reports/edge2_promoted.json`, `ai_reports/edge2_mtf_promoted.json` (empty list)

## Round 3 long-history check (2026-09-26)

**Question:** does the promoted rule "G IDX4 r1&r12 hiVol ->15:50" hold up on all the history available before the round-2 window? The rule was frozen with NO retuning.

**The rule, exactly as promoted:**
- Symbols: SPY, QQQ, IWM, DIA.
- Volatility filter: first-30-minute realized vol above the 66.7th percentile of that ETF's previous 20 qualifying days.
- Direction check at 15:30: the prior close→10:00 return and the 15:00→15:30 return must have the same sign.
- Trade in that direction from 15:30 to 15:50.
- Cost: charge the full quoted spread at 15:30.

**Data:**
- Free Alpaca IEX 1-minute bars. Months requested: 2016-01 through 2026-09.
- IEX returned nothing before **2020-07-27**. That gives about 5.2 years (1,299 trading days) of new history ahead of the 250 days round 2 used.
- Spreads, new history: SIP quotes (historical, free) at 15:31 on the first and middle trading day of each month, using the per symbol-month median. Median spread was about 0.3–0.7 bp, so cost is small here.
- Spreads, round-2 window: the round-2 SpreadModel.
- Script: `tools/studies/edge3_longhist.py` (fetch / spreads / test). Raw output is appended to `_raw.txt`.
- The job was read-only, ran at nice 15 and ≤1 request/second. No config changes, no restart, no commit.

### Per year (new history only, 15:30→15:50, net bp/trade)

| Year | Trades | /day | Net/trade | Day-weighted mean | t (day-clustered) | 95% CI (day bootstrap) | 15:30→16:00 net (t) |
|---|---|---|---|---|---|---|---|
| 2020 (from 07-27) | 45 | 0.41 | −12.2 | −8.9 | −2.31 | [−20.7, −3.6] | −22.8 (−3.06) |
| 2021 | 139 | 0.55 | −1.9 | −3.3 | −1.61 | [−6.4, +2.3] | −3.4 (−0.99) |
| 2022 | 169 | 0.67 | +3.4 | +1.0 | +0.33 | [−4.3, +10.5] | +0.4 (−1.01) |
| 2023 | 146 | 0.58 | −0.9 | −1.4 | −0.90 | [−4.6, +2.7] | +0.8 (+0.51) |
| 2024 | 155 | 0.62 | −1.3 | −1.2 | −0.70 | [−4.6, +1.8] | −2.8 (−0.24) |
| 2025 (to 09-26) | 104 | 0.57 | +0.0 | +0.0 | +0.00 | [−4.5, +4.6] | −1.0 (−0.15) |

How to read the t column: t is computed on the average of each day's trade results. The "Day-weighted mean" column is the number it tests. It can differ from net/trade on days with several trades. 2022 is the example: +3.4 bp per trade, but only +1.0 day-weighted.

### Pooled

| Slice | Trades | Net/trade | Day-weighted | t | 95% CI | 15:30→16:00 net (t) |
|---|---|---|---|---|---|---|
| **NEW history, excluding the 250 round-2 days** | **758** | **−0.8** | **−1.6** | **−1.71** | **[−3.1, +1.4]** | −2.4 (−1.89) |
| new, SPY | 243 | +0.2 | +0.2 | +0.14 | [−2.5, +3.0] | −2.2 |
| new, QQQ | 228 | +0.1 | +0.1 | +0.05 | [−2.8, +2.8] | −1.9 |
| new, IWM | 234 | −2.3 | −2.3 | −1.66 | [−4.8, +0.4] | −3.5 |
| new, DIA | 53 | −2.1 | −2.1 | −0.71 | [−8.6, +3.6] | −1.4 |
| new, 2020-07..2022 | 353 | −0.7 | −2.3 | −1.38 | [−4.4, +3.4] | −4.1 (−2.48) |
| new, 2023..2025-09 | 405 | −0.8 | −1.0 | −1.02 | [−3.1, +1.5] | −1.0 (+0.01) |
| Round-2 window (same code) | 153 | ≈+5.7 | — | — | — | — |
| ALL (new + round 2) | 911 | +0.3 | −0.7 | −0.87 | [−1.7, +2.3] | −1.0 (−1.39) |

### Reproduction check

The same code, run on the round-2 window, gives:

| Split | This code | Round 2 reported |
|---|---|---|
| Tune | +4.9 (t 2.10, n 97) | +4.3 |
| Validate | +5.8 (n 32) | +5.9 |
| Holdout | +8.9 (n 24) | +9.0 |

These are close but not identical. The likely reasons:
- The 20-day volatility history now starts in 2020 rather than at 2025-09-29, so early-tune filter decisions differ slightly.
- Bars come from the monthly re-fetch.

So the positive round-2 result reproduces. It simply does not exist outside those 250 days.

### Caveats
- IEX-only bars. IEX carries roughly 2–3% of volume, and there is no closing auction, so the 16:00 leg uses the last IEX print.
- Spreads in the new history are sampled twice a month. Even at zero cost the gross result is −0.3 bp/trade pooled, so the verdict does not depend on the spread model.
- There is no data before 2020-07 on the free IEX feed. The 2016–2019 goal could not be met without paid SIP bars, which were not used.

### Verdict: **noise.**

On 758 out-of-sample trades over about 5 years:
- The rule makes −0.8 bp/trade net (−0.3 gross), t −1.7.
- The CI straddles zero, with a slightly negative lean.
- It is positive in 1 of 6 years (2022, t 0.3).
- SPY and QQQ are flat. IWM and DIA are negative.
- The 16:00 variant is worse (−2.4 bp).

The +4–9 bp seen in round 2 is specific to that window: it shows up only in those 250 days (2025-09..2026-09) and nowhere earlier. **Recommendation:** do not promote. Drop the observe-only paper trial, or run it only as a zero-weight curiosity. No live changes.
