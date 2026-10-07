# Earnings drift (E1): UNDERPOWERED, no verdict — halves disagree; recent years negative

Pre-registration: [`earnings_drift_prereg.json`](earnings_drift_prereg.json) (amended twice; script skeptic-verified, merged e0f6384).
Benzinga EPS headlines for 2,696 eligible symbols (3,054 fetch units, 0 failures), 24,385 events; PRIMARY (EPS beat and
15:45 reaction > 0) vs a same-symbol control, 5-session beta-adjusted hold.

- Power: MDE ~60 bp per half (> 30 bp bar) -> **UNDERPOWERED, no verdict** as registered.
- Point estimates (PRIMARY minus control): H1 2019-22 **+16.3 bp** at 10 bp cost (t 0.86) / -3.8 at 30 bp; H2 2023-26
  **-24.2 bp** (t -1.39) / **-44.2 at 30 bp (t -2.53)**. Raw vs SPY: H1 +16.0, H2 -40.0.
- Plain words: no evidence of a usable post-earnings drift in liquid names; whatever existed in 2019-22 is gone or
  reversed since 2023 (consistent with the 9/27 PEAD cell, -0.40% t -3.5). The multi-day-hold question is closed for now.

## Full report
# Earnings drift (prereg docs/studies/earnings_drift_prereg.json)

## Power (SE and MDE per half, written before any mean; MDE = 2.84 x max(SE week, SE calendar))
```
{
 "H1": {
  "n": 4314,
  "se_week": 14.35,
  "se_cal": 21.26,
  "se_used": 21.26,
  "mde_bp": 60.38
 },
 "H2": {
  "n": 5526,
  "se_week": 17.47,
  "se_cal": 21.81,
  "se_used": 21.81,
  "mde_bp": 61.94
 },
 "power_ok": false
}
```

**VERDICT: UNDERPOWERED: no verdict**

PRIMARY events with no control (12 candidates tried): 2

```
{
 "prereg": "docs/studies/earnings_drift_prereg.json (7ed491c, 218199a, amended_2)",
 "event_counts": {
  "surprise_beat": 17782,
  "surprise_miss": 4661,
  "surprise_unclassified": 1937,
  "not_eligible": 27281,
  "dedup_10d": 655,
  "outside_period": 24,
  "excluded_1545_1600": 14,
  "burst_conflict": 18,
  "surprise_inline": 5,
  "news_fail_symbols": 0
 },
 "score_counts": {
  "fetch_fail": 0,
  "split_guard": 28,
  "no_return": 199,
  "no_r0": 19,
  "ctl_split_guard": 5,
  "no_control": 2
 },
 "primary": {
  "H1": {
   "n": 4314,
   "cost10": {
    "diff_bp": 16.25,
    "t_week": 1.13,
    "cal_daily_diff_bp": 3.65,
    "t_cal": 0.86,
    "t_used": 0.86
   },
   "cost30": {
    "diff_bp": -3.75,
    "t_week": -0.26,
    "cal_daily_diff_bp": -0.517,
    "t_cal": -0.12,
    "t_used": -0.26
   },
   "drop_top3_weeks_mean": 3.41,
   "drop_top5_names_mean": 10.1,
   "primary_vs_spy_net10_mean": 15.99
  },
  "H2": {
   "n": 5526,
   "cost10": {
    "diff_bp": -24.2,
    "t_week": -1.39,
    "cal_daily_diff_bp": -3.568,
    "t_cal": -0.82,
    "t_used": -1.39
   },
   "cost30": {
    "diff_bp": -44.2,
    "t_week": -2.53,
    "cal_daily_diff_bp": -7.69,
    "t_cal": -1.77,
    "t_used": -2.53
   },
   "drop_top3_weeks_mean": -39.1,
   "drop_top5_names_mean": -29.7,
   "primary_vs_spy_net10_mean": -40.02
  }
 },
 "coverage": {
  "2019": {
   "matched": 2346,
   "symbol_years": 741.8,
   "coverage": 0.791,
   "eligible_half_year_symbols": 726,
   "symbols_lt2_events": 111
  },
  "2020": {
   "matched": 2547,
   "symbol_years": 815.4,
   "coverage": 0.781,
   "eligible_half_year_symbols": 779,
   "symbols_lt2_events": 115
  },
  "2021": {
   "matched": 3009,
   "symbol_years": 993.0,
   "coverage": 0.758,
   "eligible_half_year_symbols": 954,
   "symbols_lt2_events": 175
  },
  "2022": {
   "matched": 2899,
   "symbol_years": 914.3,
   "coverage": 0.793,
   "eligible_half_year_symbols": 892,
   "symbols_lt2_events": 138
  },
  "2023": {
   "matched": 2753,
   "symbol_years": 868.1,
   "coverage": 0.793,
   "eligible_half_year_symbols": 848,
   "symbols_lt2_events": 103
  },
  "2024": {
   "matched": 3098,
   "symbol_years": 968.4,
   "coverage": 0.8,
   "eligible_half_year_symbols": 949,
   "symbols_lt2_events": 115
  },
  "2025": {
   "matched": 4024,
   "symbol_years": 1177.2,
   "coverage": 0.855,
   "eligible_half_year_symbols": 1146,
   "symbols_lt2_events": 108
  },
  "2026": {
   "matched": 3709,
   "symbol_years": 1385.2,
   "coverage": 0.669,
   "eligible_half_year_symbols": 1357,
   "symbols_lt2_events": 138
  }
 },
 "power_ok": false,
 "criteria": {
  "H1": {
   "n>=300": true,
   "diff>=20": false,
   "t>=2": false,
   "cost30>0": false,
   "drop_weeks>0": true,
   "drop_names>0": true
  },
  "H2": {
   "n>=300": true,
   "diff>=20": false,
   "t>=2": false,
   "cost30>0": false,
   "drop_weeks>0": false,
   "drop_names>0": false
  }
 },
 "verdict": "UNDERPOWERED: no verdict"
}
```

## Information
```
{
 "Misses & R0<0 (5d) H1": {
  "n": 1202,
  "mean_bp_gross": -26.15,
  "t_week": -1.21
 },
 "Misses & R0<0 (5d) H2": {
  "n": 1776,
  "mean_bp_gross": -14.07,
  "t_week": -0.62
 },
 "all Beats (5d) H1": {
  "n": 7709,
  "mean_bp_gross": 15.4,
  "t_week": 1.38
 },
 "all Beats (5d) H2": {
  "n": 9922,
  "mean_bp_gross": -29.51,
  "t_week": -2.28
 },
 "R0>0 any surprise (5d) H1": {
  "n": 5484,
  "mean_bp_gross": 21.63,
  "t_week": 1.75
 },
 "R0>0 any surprise (5d) H2": {
  "n": 6844,
  "mean_bp_gross": -26.12,
  "t_week": -1.84
 },
 "PRIMARY 20d H1": {
  "n": 4314,
  "mean_bp_gross": 39.5,
  "t_week": 1.53
 },
 "PRIMARY 20d H2": {
  "n": 5503,
  "mean_bp_gross": -61.55,
  "t_week": -2.71
 },
 "PRIMARY 1d H1": {
  "n": 4315,
  "mean_bp_gross": 7.94,
  "t_week": 1.6
 },
 "PRIMARY 1d H2": {
  "n": 5527,
  "mean_bp_gross": -2.89,
  "t_week": -0.48
 },
 "PRIMARY raw vs SPY 5d H1": {
  "n": 4315,
  "mean_bp_gross": 29.95,
  "t_week": 2.55
 },
 "PRIMARY raw vs SPY 5d H2": {
  "n": 5527,
  "mean_bp_gross": -27.99,
  "t_week": -1.9
 },
 "PRIMARY H1-style, active names only (5d) H1": {
  "n": 4202,
  "mean_bp_gross": 26.14,
  "t_week": 2.23
 },
 "PRIMARY H1-style, active names only (5d) H2": {
  "n": 5523,
  "mean_bp_gross": -30.01,
  "t_week": -2.05
 },
 "PRIMARY inactive names only (5d) H1": {
  "n": 113,
  "mean_bp_gross": 22.21,
  "t_week": 0.67
 },
 "events_per_year": {
  "2019": 2314,
  "2020": 2516,
  "2021": 2941,
  "2022": 2877,
  "2023": 2741,
  "2024": 3080,
  "2025": 4001,
  "2026": 3669
 },
 "unclassified_per_year": {
  "2019": 262,
  "2020": 307,
  "2021": 248,
  "2022": 260,
  "2023": 232,
  "2024": 202,
  "2025": 224,
  "2026": 163
 }
}
```
