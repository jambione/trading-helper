# Bollinger Bands at the −50 cross — ranking input? (2026-09-25)

**Market open; mini read-only.** Script run from `/tmp` on the mini with `nice -n 15`; bars from the local SIP cache (`runway_bars_cache.pkl`, 9/1–9/24, nothing from today). One small daily-volume pull (3 multi-symbol requests, 1/sec) for volume pace, cached to `/tmp/bb_avgvol.json`. No code, config or process changes on the mini.

## Plain English

We checked whether Bollinger Bands tell us which names to seat after the fast %R crosses up through −50. **Where the price sits inside the bands (%B) tells us nothing useful**: names at the top of or above their bands did no worse than names in the middle, and the small differences flip between the two date halves. **How wide the bands are** looks a bit better: names with wide 5-minute bands (big recent swings) went on to do slightly better in both halves, but it's weak once each stock-day counts once, and it didn't improve a held-out ranking. The clearest result isn't Bollinger at all: **names with more room below the day's high did clearly better** than names sitting near their high. That holds in both halves, when each stock-day counts once, and on every day we could check (0 of 7 days went the other way). **Recommendation: don't add %B or bandwidth to the book server ranking.** If the ranking gets something new, it should be room below the day's high, which is easy to compute live from 1-minute highs.

## Setup

- **Events:** 3,073 fast-%R −50 upward crosses (train 878 crosses / 122 symbol-days, ≤ 9/17; test 2,195 / 352, 9/18–9/24). These are the same population and labels as `mid_rise_runway_study` / `day_change_fade_study` (the functions are imported, not rewritten): supply names, $20–100, 09:35–15:30 ET.
- **Labels:** runner (≥0.35R or ≥0.6% within 60m before −1R), ret30/ret60 (close to close), fade (−1% before +1% within 60m).
- **Features, using bars through the cross minute only.** The cross minute's close is what fires the signal and is the entry price.
  - `pctb_1m`, `bw_1m`: BB(20, 2σ) on 1m closes. %B = (close − lower)/(upper − lower); bandwidth = (upper − lower)/middle.
  - `pctb_5m`, `bw_5m`: BB(20, 2) on clock-aligned 5m closes built from 1m. The last 5m bar is the one still forming, with close = the cross price, the way a live chart shows it. The cache is RTH-only, so this needs 100 minutes of the session: it exists for 2,523 of 3,073 crosses, none before ~11:05.
  - `squeeze_1m`: bw_1m ÷ the median bw_1m over earlier bars that day. Below 1 means the bands are tighter than usual for that name today.
  - `dist_hod_pct`: (price ÷ high of day so far − 1)·100, i.e. room below HOD (more negative = more room).
  - `nw_pos`: a **causal** Nadaraya-Watson envelope position (see §6).
  - `day_chg_pct` and `rvol_pace` follow `day_change_fade_study` (pace available on 3,056 crosses).
- **Symbol-day check:** every bucket shows how many distinct symbol-days it holds and the share of its crosses that come from its top-2 symbol-days (⚠ if ≥40% or <8 symbol-days). "SD" columns average within each symbol-day first, then across symbol-days. No bucket with real n was dominated by one or two names: top-2 shares are 1–9% everywhere except the tiny %B <0.2 and 5m %B >1.0 buckets.
- The test half runs ~+0.3% ret60 above train in every bucket (market drift), so compare **within a half**, never across halves.

## Verdict table (tercile top vs bottom; train cuts applied to both halves)

| Feature | ret60 T3−T1 train / test (t) | Same sign per-cross? | By symbol-day train / test (t) | Within day_chg×pace strata (SD) | Days T3>T1 | Verdict |
|---|---|---|---|---|---|---|
| %B 1m | +0.03 (+0.3) / −0.02 (−0.3) | FLIP | −0.02 (−0.2) / −0.10 (−0.8) | +0.08 / −0.10 | 4/10 | **No evidence** |
| %B 5m | −0.14 (−1.5) / +0.04 (+0.6) | FLIP | −0.37 (−3.2) / −0.24 (−2.4) | −0.29 / −0.21 | 0/7 | No evidence as its own input (by symbol-day it leans negative, but per cross it flips; ρ≈0.4 with room-below-HOD, so it's mostly that) |
| Bandwidth 1m | −0.05 (−0.5) / +0.19 (+2.0) | FLIP | −0.17 (−1.3) / +0.10 (+0.9) | −0.19 / +0.13 | 4/7 | **No evidence** (big runner lift is mechanical: wider bars reach +0.6% more often) |
| Bandwidth 5m | +0.23 (+2.3) / +0.28 (+3.3) | same | +0.24 (+2.1) / +0.16 (+1.4) | +0.30 / +0.16 | 5/7 | Same sign everywhere, weak on test by symbol-day; held-out rank not improved → **not proven** |
| Squeeze 1m (bw ÷ own-day median) | −0.36 (−4.1) / −0.17 (−2.1) | same | −0.34 (−2.9) / −0.22 (−1.8) | −0.33 / −0.23 | 4/10 | Leans "calmer-than-its-day is better", but day by day it's a coin flip → **not proven** |
| Room below HOD | −0.11 (−1.2) / −0.36 (−3.8) | same | −0.51 (−2.8) / −0.84 (−3.7) | −0.19 / −0.59 | **0/7** | **Real**: near-HOD names are worse, and it's the strongest input here |

(T3 = highest values. For dist_hod, T3 = nearest the high, ≥ −1.2%; T1 = ≥2.7% below the high.)

**Held-out rank** (linear model on ret60, fit on one half and scored on the other, both directions). Day change + pace alone: test Spearman +0.040. Adding %B 1m: +0.035. Adding bw 1m: −0.000. Adding %B+bw 5m: +0.002. Adding squeeze: +0.025. Reverse direction: the base model is ~0, and 5m BB (+0.14) and squeeze (+0.12) help there. So the two directions disagree, and nothing BB-based improves the ranking consistently. Full tables below.

**%B vs room below HOD:** Spearman ρ = +0.05 / −0.01 (1m) and +0.38 / +0.40 (5m) for train / test. 1m %B is unrelated to HOD room. 5m %B partly overlaps it.

## Recommendation (evidence only)

- **%B (1m or 5m): do not add.** No bucket holds in both halves by symbol-day, and %B > 1.0 is **not** worse (test: runner 52.6%, fade 38.7%, the best bucket), so no "penalize > 1.0" shape either.
- **Bandwidth: do not add yet.** 5m bandwidth is the only BB measure with the same sign in every cut, but the test-half symbol-day t is 1.4 and it didn't improve a held-out rank. It's worth logging in shadow. Note that it runs *against* the "calmer names" idea on the 5m scale, while the 1m squeeze leans toward it; neither is solid.
- **Room below HOD is the finding.** More room below the day's high → better ret60 and runner rate in both halves, by symbol-day, within day_chg×pace strata, and on 7/7 days. It was dropped only because nothing live computes it; it needs just the running 1m high. It's worth a separate replay by symbol-day before adding it to the ranking.

## §6 Nadaraya-Watson envelope

The standard TradingView "Nadaraya-Watson Envelope" **repaints**: its default mode smooths each bar with a kernel over *future* bars too, so past values change as new bars arrive. It was **excluded** for that reason. We tested only a causal version (Gaussian kernel h=8 over the last 50 closes, past bars only; envelope ±3 × mean abs deviation). Its position is highly correlated with 1m %B (ρ≈0.77) and adds nothing: ret60 T3−T1 −0.16 (−1.7) / −0.02 (−0.3), and the runner sign flips. **No evidence.**

## Reproduce

    # MacBook → mini /tmp (supply file lives on the MacBook)
    scp tools/studies/bollinger_cross_study.py ai_reports/supply_first_seen.json mac-mini-away:/tmp/
    ssh mac-mini-away 'cd ~/repo/trading-helper && PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies \
      nice -n 15 .venv/bin/python /tmp/bollinger_cross_study.py --fetch-daily' < /dev/null

---

## Full output (2026-09-25 run)

events=3073  train days=['2026-09-01', '2026-09-03', '2026-09-04', '2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11', '2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17']  test days=['2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24']
skipped={'price_band': 7271, 'outside_rth': 991, 'before_source': 571, 'not_in_supply': 418, 'short_path': 96}
rvol_pace available: 3056
coverage pctb_1m: 3073
coverage bw_1m: 3073
coverage pctb_5m: 2523
coverage bw_5m: 2523
coverage squeeze_1m: 3021
coverage nw_pos: 2791
coverage dist_hod_pct: 3073

## Correlations (Spearman, all crosses)

| Pair | train | test |
|---|--:|--:|
| pctb_1m vs dist_hod_pct | +0.05 | -0.01 |
| pctb_5m vs dist_hod_pct | +0.38 | +0.40 |
| pctb_1m vs pctb_5m | +0.21 | +0.21 |
| bw_1m vs bw_5m | +0.64 | +0.68 |
| bw_1m vs day_chg_pct | +0.18 | +0.12 |
| bw_1m vs rvol_pace | +0.39 | +0.21 |
| pctb_1m vs day_chg_pct | -0.01 | -0.03 |
| pctb_1m vs rvol_pace | -0.04 | -0.01 |
| nw_pos vs pctb_1m | +0.76 | +0.78 |

## Buckets

SD runner / SD ret60 = average within each symbol-day first, then across symbol-days. ⚠ = top-2 symbol-days ≥40% of crosses or <8 symbol-days.

### pctb_1m

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 878 | 122 | 3% | 46.0% | -0.076% | -0.158% | 56.7% | 45.4% | -0.205% |
| ALL | test | 2195 | 352 | 1% | 47.2% | +0.072% | +0.119% | 44.7% | 48.9% | +0.147% |
| <0.2 | train | 1 | 1 | 100% ⚠ | 0.0% | +0.010% | -0.154% | nan% | 0.0% | -0.154% |
| <0.2 | test | 1 | 1 | 100% ⚠ | 0.0% | -0.114% | -0.081% | nan% | 0.0% | -0.081% |
| 0.2-0.5 | train | 25 | 21 | 16% | 32.0% | -0.220% | -0.494% | 78.6% | 31.0% | -0.469% |
| 0.2-0.5 | test | 66 | 57 | 9% | 43.9% | +0.213% | +0.151% | 63.6% | 43.6% | +0.204% |
| 0.5-0.8 | train | 402 | 115 | 5% | 47.8% | -0.016% | -0.108% | 54.5% | 48.1% | -0.118% |
| 0.5-0.8 | test | 1000 | 305 | 2% | 45.1% | +0.051% | +0.100% | 45.2% | 46.8% | +0.161% |
| 0.8-1.0 | train | 278 | 108 | 4% | 45.3% | -0.083% | -0.181% | 56.2% | 46.0% | -0.206% |
| 0.8-1.0 | test | 685 | 290 | 2% | 47.2% | +0.060% | +0.136% | 46.4% | 49.7% | +0.206% |
| >1.0 | train | 172 | 94 | 5% | 45.3% | -0.184% | -0.187% | 59.0% | 43.4% | -0.249% |
| >1.0 | test | 443 | 253 | 2% | 52.6% | +0.119% | +0.132% | 38.7% | 53.6% | +0.085% |

(missing pctb_1m: 0 of 3073)

### pctb_5m

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 727 | 122 | 3% | 43.1% | -0.108% | -0.216% | 59.2% | 43.1% | -0.253% |
| ALL | test | 1796 | 350 | 1% | 44.4% | +0.046% | +0.124% | 44.5% | 47.5% | +0.149% |
| <0.2 | train | 23 | 21 | 17% | 65.2% | +0.147% | -0.057% | 38.5% | 66.7% | -0.077% |
| <0.2 | test | 47 | 43 | 9% | 44.7% | +0.110% | +0.136% | 42.9% | 46.5% | +0.169% |
| 0.2-0.5 | train | 323 | 109 | 4% | 42.1% | -0.070% | -0.165% | 57.1% | 45.5% | -0.101% |
| 0.2-0.5 | test | 784 | 292 | 2% | 41.7% | +0.025% | +0.100% | 47.8% | 50.7% | +0.295% |
| 0.5-0.8 | train | 278 | 101 | 5% | 43.9% | -0.138% | -0.265% | 61.2% | 40.7% | -0.317% |
| 0.5-0.8 | test | 673 | 274 | 2% | 46.1% | +0.073% | +0.184% | 40.4% | 44.8% | +0.161% |
| 0.8-1.0 | train | 93 | 57 | 9% | 38.7% | -0.161% | -0.210% | 62.7% | 38.0% | -0.224% |
| 0.8-1.0 | test | 257 | 171 | 3% | 47.9% | +0.011% | +0.021% | 48.1% | 48.0% | -0.055% |
| >1.0 | train | 10 | 10 | 20% | 40.0% | -0.606% | -0.956% | 75.0% | 40.0% | -0.956% |
| >1.0 | test | 35 | 32 | 14% | 48.6% | +0.189% | +0.238% | 37.5% | 51.0% | +0.266% |

(missing pctb_5m: 550 of 3073)

### bw_1m

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 878 | 122 | 3% | 46.0% | -0.076% | -0.158% | 56.7% | 45.4% | -0.205% |
| ALL | test | 2195 | 352 | 1% | 47.2% | +0.072% | +0.119% | 44.7% | 48.9% | +0.147% |
| T1 <0.0041 | train | 292 | 87 | 7% | 29.1% | +0.005% | -0.108% | 51.5% | 39.3% | -0.081% |
| T1 <0.0041 | test | 856 | 242 | 3% | 30.0% | +0.025% | +0.027% | 44.2% | 37.7% | +0.044% |
| T2 | train | 293 | 109 | 5% | 43.7% | -0.166% | -0.210% | 61.3% | 43.3% | -0.187% |
| T2 | test | 717 | 253 | 3% | 51.2% | +0.062% | +0.146% | 41.8% | 51.4% | +0.202% |
| T3 ≥0.0073 | train | 293 | 91 | 8% | 65.2% | -0.066% | -0.154% | 54.9% | 61.4% | -0.248% |
| T3 ≥0.0073 | test | 622 | 219 | 3% | 66.2% | +0.150% | +0.216% | 46.7% | 65.0% | +0.146% |

(missing bw_1m: 0 of 3073)
(tercile cuts from train: [0.0040944719593425105, 0.007268302303657745])

### bw_5m

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 727 | 122 | 3% | 43.1% | -0.108% | -0.216% | 59.2% | 43.1% | -0.253% |
| ALL | test | 1796 | 350 | 1% | 44.4% | +0.046% | +0.124% | 44.5% | 47.5% | +0.149% |
| T1 <0.0110 | train | 242 | 72 | 6% | 22.3% | -0.080% | -0.282% | 70.0% | 29.6% | -0.309% |
| T1 <0.0110 | test | 715 | 214 | 3% | 28.7% | +0.017% | +0.009% | 47.0% | 34.3% | +0.052% |
| T2 | train | 242 | 91 | 6% | 44.2% | -0.239% | -0.314% | 64.2% | 43.2% | -0.304% |
| T2 | test | 553 | 236 | 3% | 45.6% | +0.033% | +0.109% | 49.2% | 45.0% | +0.177% |
| T3 ≥0.0203 | train | 243 | 83 | 8% | 62.6% | -0.005% | -0.054% | 51.4% | 58.6% | -0.064% |
| T3 ≥0.0203 | test | 528 | 210 | 3% | 64.6% | +0.101% | +0.293% | 41.3% | 62.5% | +0.213% |

(missing bw_5m: 550 of 3073)
(tercile cuts from train: [0.01098379916103744, 0.02028298233870339])

### squeeze_1m

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 865 | 122 | 3% | 45.8% | -0.078% | -0.163% | 56.7% | 45.3% | -0.211% |
| ALL | test | 2156 | 352 | 1% | 46.7% | +0.074% | +0.123% | 44.9% | 48.2% | +0.146% |
| T1 <0.4819 | train | 288 | 104 | 5% | 42.4% | -0.051% | -0.027% | 50.0% | 44.0% | -0.044% |
| T1 <0.4819 | test | 740 | 287 | 2% | 47.8% | +0.173% | +0.266% | 40.3% | 50.1% | +0.295% |
| T2 | train | 288 | 101 | 5% | 46.9% | +0.017% | -0.071% | 50.9% | 44.8% | -0.149% |
| T2 | test | 723 | 286 | 3% | 44.1% | -0.010% | +0.003% | 51.7% | 48.1% | +0.043% |
| T3 ≥0.6777 | train | 289 | 112 | 4% | 48.1% | -0.200% | -0.389% | 66.1% | 49.9% | -0.379% |
| T3 ≥0.6777 | test | 693 | 289 | 2% | 48.1% | +0.056% | +0.094% | 42.5% | 50.1% | +0.077% |

(missing squeeze_1m: 52 of 3073)
(tercile cuts from train: [0.4819320021254511, 0.677719462911805])

### nw_pos

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 797 | 122 | 3% | 44.2% | -0.108% | -0.191% | 58.4% | 44.0% | -0.225% |
| ALL | test | 1994 | 349 | 1% | 45.7% | +0.071% | +0.129% | 44.2% | 48.1% | +0.155% |
| T1 <0.2551 | train | 265 | 105 | 5% | 44.9% | -0.032% | -0.107% | 55.8% | 43.2% | -0.098% |
| T1 <0.2551 | test | 697 | 287 | 2% | 43.6% | +0.028% | +0.103% | 46.5% | 44.3% | +0.165% |
| T2 | train | 266 | 107 | 5% | 43.2% | -0.114% | -0.198% | 59.7% | 45.9% | -0.236% |
| T2 | test | 667 | 284 | 2% | 46.5% | +0.134% | +0.196% | 41.0% | 50.0% | +0.331% |
| T3 ≥0.4664 | train | 266 | 109 | 5% | 44.4% | -0.177% | -0.267% | 59.5% | 44.0% | -0.256% |
| T3 ≥0.4664 | test | 630 | 285 | 2% | 47.3% | +0.053% | +0.087% | 45.5% | 49.5% | +0.088% |

(missing nw_pos: 282 of 3073)
(tercile cuts from train: [0.255087354110411, 0.466351452258722])

### dist_hod_pct

| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 | Fade | SD runner | SD ret60 |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| ALL | train | 878 | 122 | 3% | 46.0% | -0.076% | -0.158% | 56.7% | 45.4% | -0.205% |
| ALL | test | 2195 | 352 | 1% | 47.2% | +0.072% | +0.119% | 44.7% | 48.9% | +0.147% |
| T1 <-2.7441 | train | 292 | 60 | 8% | 56.5% | +0.010% | -0.075% | 50.3% | 61.0% | +0.102% |
| T1 <-2.7441 | test | 602 | 136 | 4% | 60.5% | +0.238% | +0.359% | 43.3% | 67.9% | +0.613% |
| T2 | train | 293 | 85 | 7% | 45.4% | -0.130% | -0.209% | 62.3% | 46.6% | -0.295% |
| T2 | test | 661 | 191 | 4% | 44.6% | +0.013% | +0.063% | 45.2% | 54.0% | +0.100% |
| T3 ≥-1.1996 | train | 293 | 69 | 6% | 36.2% | -0.108% | -0.188% | 60.0% | 36.9% | -0.410% |
| T3 ≥-1.1996 | test | 932 | 220 | 3% | 40.5% | +0.007% | +0.004% | 45.6% | 41.6% | -0.228% |

(missing dist_hod_pct: 0 of 3073)
(tercile cuts from train: [-2.744073175284667, -1.199557086614167])

## Tercile top vs bottom (train cuts applied to both halves)

| Feature | Half | n top/bot (sym-days) | ret60 T3−T1 (t) | SD ret60 T3−T1 (t) | runner T3−T1 (t) | SD runner (t) | fade T3−T1 (t) |
|---|---|---|--:|--:|--:|--:|--:|
| pctb_1m | train | 293/292 (112/108) | +0.027% (+0.3) | -0.022% (-0.2) | +1.9% (+0.5) | -1.9% (-0.4) | -1.1% (-0.2) |
| pctb_1m | test | 787/706 (312/269) | -0.021% (-0.3) | -0.099% (-0.8) | +6.3% (+2.4) | +6.7% (+2.0) | -4.1% (-1.0) |
| pctb_1m | sign | ret_60:FLIP ret_60_sd:same runner:same runner_sd:FLIP fade:same | | | | | |
| pctb_5m | train | 243/242 (93/105) | -0.139% (-1.5) | -0.372% (-3.2) | -1.8% (-0.4) | -10.9% (-1.9) | +10.0% (+1.6) |
| pctb_5m | test | 636/573 (255/248) | +0.039% (+0.6) | -0.235% (-2.4) | +5.9% (+2.1) | -4.4% (-1.1) | -1.0% (-0.2) |
| pctb_5m | sign | ret_60:FLIP ret_60_sd:same runner:FLIP runner_sd:same fade:FLIP | | | | | |
| bw_1m | train | 293/292 (91/87) | -0.046% (-0.5) | -0.168% (-1.3) | +36.1% (+9.4) | +22.1% (+3.8) | +3.4% (+0.5) |
| bw_1m | test | 622/856 (219/242) | +0.189% (+2.0) | +0.102% (+0.9) | +36.2% (+14.7) | +27.4% (+7.6) | +2.6% (+0.6) |
| bw_1m | sign | ret_60:FLIP ret_60_sd:FLIP runner:same runner_sd:same fade:same | | | | | |
| bw_5m | train | 243/242 (83/72) | +0.227% (+2.3) | +0.244% (+2.1) | +40.2% (+9.8) | +29.0% (+4.7) | -18.6% (-2.8) |
| bw_5m | test | 528/715 (210/214) | +0.284% (+3.3) | +0.161% (+1.4) | +35.9% (+13.4) | +28.2% (+7.2) | -5.7% (-1.1) |
| bw_5m | sign | ret_60:same ret_60_sd:same runner:same runner_sd:same fade:same | | | | | |
| squeeze_1m | train | 289/288 (112/104) | -0.362% (-4.1) | -0.335% (-2.9) | +5.7% (+1.4) | +5.9% (+1.1) | +16.1% (+2.8) |
| squeeze_1m | test | 693/740 (289/287) | -0.171% (-2.1) | -0.218% (-1.8) | +0.2% (+0.1) | -0.0% (-0.0) | +2.2% (+0.5) |
| squeeze_1m | sign | ret_60:same ret_60_sd:same runner:same runner_sd:FLIP fade:same | | | | | |
| nw_pos | train | 266/265 (109/105) | -0.159% (-1.7) | -0.158% (-1.3) | -0.5% (-0.1) | +0.8% (+0.2) | +3.7% (+0.6) |
| nw_pos | test | 630/697 (285/287) | -0.016% (-0.3) | -0.077% (-0.9) | +3.7% (+1.3) | +5.2% (+1.5) | -1.1% (-0.2) |
| nw_pos | sign | ret_60:same ret_60_sd:same runner:FLIP runner_sd:same fade:FLIP | | | | | |
| dist_hod_pct | train | 293/292 (69/60) | -0.113% (-1.2) | -0.512% (-2.8) | -20.3% (-5.0) | -24.1% (-3.9) | +9.7% (+1.7) |
| dist_hod_pct | test | 932/602 (220/136) | -0.355% (-3.8) | -0.842% (-3.7) | -20.0% (-7.8) | -26.3% (-6.6) | +2.4% (+0.6) |
| dist_hod_pct | sign | ret_60:same ret_60_sd:same runner:same runner_sd:same fade:same | | | | | |
| day_chg_pct | train | 293/292 (49/59) | +0.057% (+0.6) | -0.201% (-1.2) | +10.8% (+2.6) | +0.6% (+0.1) | +2.0% (+0.4) |
| day_chg_pct | test | 566/653 (141/130) | -0.072% (-0.7) | -0.520% (-2.3) | +7.0% (+2.5) | -5.5% (-1.2) | +0.5% (+0.1) |
| day_chg_pct | sign | ret_60:FLIP ret_60_sd:same runner:same runner_sd:FLIP fade:same | | | | | |
| rvol_pace | train | 293/292 (49/46) | +0.210% (+2.2) | +0.076% (+0.5) | +24.4% (+6.1) | +19.4% (+2.7) | -7.1% (-1.3) |
| rvol_pace | test | 588/768 (112/160) | +0.125% (+1.3) | -0.066% (-0.3) | +15.6% (+5.7) | +11.4% (+2.5) | -1.8% (-0.4) |
| rvol_pace | sign | ret_60:same ret_60_sd:FLIP runner:same runner_sd:same fade:same | | | | | |

## Within day_chg × pace strata (T3−T1 ret60, cells with ≥5 each side)

| Feature | Half | cells | cells T3>T1 | weighted ret60 diff | SD-weighted diff |
|---|---|--:|--:|--:|--:|
| pctb_1m | train | 9 | 6/9 | +0.027% | +0.078% |
| pctb_1m | test | 9 | 5/9 | -0.042% | -0.100% |
| pctb_5m | train | 9 | 3/9 | -0.165% | -0.289% |
| pctb_5m | test | 9 | 4/9 | +0.009% | -0.206% |
| bw_1m | train | 9 | 4/9 | -0.181% | -0.194% |
| bw_1m | test | 9 | 7/9 | +0.153% | +0.132% |
| bw_5m | train | 9 | 6/9 | +0.278% | +0.302% |
| bw_5m | test | 9 | 4/9 | +0.198% | +0.158% |
| squeeze_1m | train | 9 | 3/9 | -0.369% | -0.327% |
| squeeze_1m | test | 9 | 4/9 | -0.146% | -0.227% |
| dist_hod_pct | train | 4 | 2/4 | -0.079% | -0.186% |
| dist_hod_pct | test | 6 | 0/6 | -0.320% | -0.589% |

## Per-day consistency: symbol-day-weighted ret60 T3−T1 (train cuts), days with ≥3 sym-days each side

| Feature | days T3>T1 / days | median daily diff |
|---|--:|--:|
| pctb_1m | 4/10 | -0.106% |
| pctb_5m | 0/7 | -0.317% |
| bw_1m | 4/7 | +0.150% |
| bw_5m | 5/7 | +0.151% |
| squeeze_1m | 4/10 | -0.073% |
| dist_hod_pct | 0/7 | -0.684% |

## Held-out linear rank: fit on train, score on test (target ret60, clipped ±5%)

| Model | n train/test | test Spearman(pred, ret60) | test top−bottom third ret60 | SD-weighted top−bottom | test top-third runner | bottom-third runner |
|---|---|--:|--:|--:|--:|--:|
| day_chg + pace (β=-0.008, +0.110) | 878/2178 | +0.040 | +0.121% | +0.049% | 54.4% | 42.0% |
| + pctb_1m (β=-0.008, +0.110, -0.005) | 878/2178 | +0.035 | +0.111% | -0.014% | 54.8% | 42.1% |
| + bw_1m (β=-0.006, +0.131, -0.051) | 878/2178 | -0.000 | -0.032% | -0.107% | 47.9% | 53.0% |
| + pctb_1m+bw_1m (β=-0.006, +0.131, -0.006, -0.051) | 878/2178 | -0.004 | -0.044% | -0.087% | 47.4% | 53.3% |
| + pctb_5m+bw_5m (β=-0.005, +0.172, -0.075, -0.042) | 727/1781 | +0.002 | +0.012% | +0.155% | 45.3% | 47.1% |
| + squeeze_1m (β=-0.008, +0.107, -0.184) | 865/2140 | +0.025 | +0.154% | +0.147% | 50.1% | 44.6% |
| + dist_hod_pct (β=-0.025, +0.124, +0.029) | 878/2178 | +0.019 | +0.098% | -0.056% | 54.4% | 46.3% |

Reverse direction (fit test, score train):

| Model | n train/test | test Spearman(pred, ret60) | test top−bottom third ret60 | SD-weighted top−bottom | test top-third runner | bottom-third runner |
|---|---|--:|--:|--:|--:|--:|
| day_chg + pace (β=-0.049, +0.003) | 2178/878 | +0.001 | -0.049% | +0.191% | 45.1% | 55.3% |
| + pctb_1m (β=-0.048, +0.002, +0.034) | 2178/878 | +0.005 | -0.009% | +0.255% | 45.4% | 50.9% |
| + bw_1m (β=-0.051, +0.001, +0.009) | 2178/878 | -0.005 | -0.047% | +0.282% | 46.8% | 53.9% |
| + pctb_1m+bw_1m (β=-0.050, +0.000, +0.034, +0.008) | 2178/878 | +0.004 | -0.027% | +0.188% | 47.8% | 50.5% |
| + pctb_5m+bw_5m (β=+0.010, +0.038, -0.001, +0.041) | 1781/727 | +0.140 | +0.346% | +0.367% | 62.0% | 17.4% |
| + squeeze_1m (β=-0.046, -0.002, -0.038) | 2140/865 | +0.121 | +0.193% | +0.366% | 39.9% | 52.1% |
| + dist_hod_pct (β=+0.030, -0.050, -0.147) | 2178/878 | +0.002 | -0.002% | +0.466% | 54.6% | 36.9% |
