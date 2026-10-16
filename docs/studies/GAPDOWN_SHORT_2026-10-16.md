# Gap-down short result 2026-10-16: FAIL (both halves)

Prereg docs/studies/gapdown_short_prereg.json + amended_1 + amended_2 (skeptic READY before data). 2,692 sessions 2016-02..2026-10, 3,640 liquid names (active + inactive), 0 failed symbols.

Shorting <= -1% gaps from the 09:31 open loses net in both halves (-29.6 / -15.5 bp/day). Against flat-gap names it is -10.8 bp (t -3.0) in 2016-2020 (gap-downs bounce) and +3.7 (t 0.8) in 2021+. SPY-hedged, 25 bp: -34.5 / -20.4. The look-ahead version (official open, info only) reads +47.6 bp/trade, which is the fake edge the skeptic's round-1 #1 warned about. if_fail: the gap-down signal stays an avoid-filter only (the live long-side gap-down block is unchanged; 2021+ gap-downs trail flat names by a weak 3.7 bp/day).

# Gap-down short (prereg amended_1+2)

Short <= -1% gaps (09:30 close vs prior close; > -10%, no SSR), top 20/day, 09:31 open -> close. Control = all liquid names gapping within +-1%, same timing. Cost 20 bp (stress 25); t bar 2.0 day-clustered.

Data: {'small_universe_days': 0, 'no_0930_0931_bar': 888216, 'no_daily_bar': 10, 'failed_syms': 0, 'no_signal_days': 0}

| half | signal days | names | gross bp | net bp (t) | paired gap-ctrl bp (t) | SPY-hedged @25 bp (t) | pos months @25 | drop top3d/top5n bp | a b c d | verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2016-01-01..2021-01-01 | 1238 | 23127 | -9.6 | -29.6 (-6.6) | -10.8 (-3.0) | -34.5 (-9.2) | 12% | -13.3 | nnnn | FAIL |
| 2021-01-01..2026-10-16 | 1453 | 28550 | +4.5 | -15.5 (-2.8) | +3.7 (+0.8) | -20.4 (-4.5) | 30% | +0.7 | nnnY | FAIL |

**Overall: FAIL** (PASS needs both halves).

## Information only

- gap -0.02..-0.01: 10292 trades, gross +0.3 bp/trade
- gap -0.04..-0.02: 22109 trades, gross -1.8 bp/trade
- gap -0.1..-0.04: 19276 trades, gross -4.2 bp/trade
- prior close $ 10..20: 11422 trades, gross -1.9 bp/trade
- prior close $ 20..50: 18740 trades, gross -12.2 bp/trade
- prior close $ 50..1000000000.0: 19957 trades, gross +5.7 bp/trade
- open-to-close (official open, look-ahead, info only): gross +47.6 bp/trade
- paired gap-minus-control by year (bp/day): 2016 -19.0, 2017 -7.9, 2018 -10.8, 2019 -11.7, 2020 -5.2, 2021 +9.8, 2022 +8.0, 2023 -2.0, 2024 -2.4, 2025 +13.5, 2026 -7.2
- borrow today: 19% of 3015 still-listed picked symbols are NOT easy-to-borrow (8 picked symbols no longer listed)
- Survivorship: tickers later reused by another company are lost; delisted names otherwise included (ACTIVE + INACTIVE assets).
