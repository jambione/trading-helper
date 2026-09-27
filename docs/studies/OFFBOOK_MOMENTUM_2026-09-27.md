# Off-book momentum names at a $1 floor: would they give runway if seated? (2026-09-27)

Question (Jonathan): test the momentum names that are NOT on the book to see whether they'd produce a runway we
could capture if they were on the book, with a $1 floor.

**Short answer: no.** At a $1 floor the off-book momentum names do not offer capturable runway at any price tier or
time window once the real SIP spread is paid. Below $10 they are clearly worse than nothing. $20-$100 and >$100 lose
only a few bp. The seated names do no better at 30 minutes. No blocker group is net positive with a usable sample.
The one lead is **holding >$100 off-book names to 15:50** (about +10 bp net, day t about 2, positive in both halves).
But that is not runway, those names sit outside the momentum $100 cap on purpose, and it is one cell out of many tested.

Script: `tools/studies/offbook_momentum_study.py` (`fetch`, then `analyze`; read-only, writes /tmp/offbook).
Full output: `OFFBOOK_MOMENTUM_2026-09-27_raw.txt`.

## Data and method
- **Universe:** every name-day with a momentum-monitor journal mention event (`new`/`burst`, 04:00-15:30), 18 sessions
  9/1-9/25. Journal events are free of the book echo; dashboard `src=book` rows are not used. Common stock only
  (`ticker_filters.is_common`, `is_levered_etp` using Alpaca asset names). SIP price at the first mention must be
  >= $1 (last 1m close before the mention, or the previous close if there is none). The tier is set by that price.
- **Groups:** **OFF** = never seeded (proposal_ledger seed/kept, any proposer) and never seated (shadow.jsonl, any
  source) that day: 709 name-days. **ON** = seated that day: 609. **SEED_ONLY** = seeded but never seated: 328
  (9/16+ only). These are also "not on the book", so they are shown with OFF as NOT_ON. **ON_post** = ON entries after
  the seat time only.
- **Dropped name-days:** 274 priced < $1 at the mention, 95 with no SIP bars (mostly OTC/foreign), 96 with no SIP
  quote sample, 2 with no RTH minute after the mention.
- **Entries:** decisions at 1m bar closes from max(mention, 09:31) to 15:30, RTH only. Fill = next bar open + half the
  SIP quoted spread (nearest of 5 samples: 09:36/09:46/10:15/12:30/15:15, each the median over a 10 s window).
  - **arm** = the desk arm (fast %R up through -50 with slow %R rising; the prior `crosses_on`, premarket-warmed).
  - **rand** = up to 20 random eligible minutes per name-day.
  - **first** = the first eligible RTH minute after the mention.
- **Metrics:**
  - +20/+35/+50 bp before -35 within 30 min (lows first). **rev50** = -50 before +35, the reverse check.
  - **end30** = the 30-min close, net of the full spread (the exit half uses the exit-time sample).
  - **hold** = exit at the 15:50 price, net of the full spread. Gross = next open to exit with no costs.
  - **SPY** = SPY's gross move over the same minutes, as a beta control.
  - t = mean of day means / SE over days. A/B = alternate days starting 9/1 (out-of-sample halves).

## Main table (bp; mean unless noted; t over days)
| tier | OFF name-days | OFF arm: +50 / rev50 | OFF arm end30 net (t) | OFF rand end30 net (t) | OFF rand hold net mean / median | ON name-days | ON arm end30 net (t) | ON rand end30 net (t) | ON rand hold net mean / median | median spread OFF / ON (rand) |
|---|---|---|---|---|---|---|---|---|---|---|
| $1-5 | 148 | 6.9% / 74.9% | -108 (-4.6) | -210 (-7.9) | -308 / -214 | 145 | -105 (-1.1) | -131 (-0.9) | +143 / -183 * | 124 / 85 |
| $5-10 | 94 | 21.9 / 47.6 | -32 (-2.5) | -54 (-2.9) | -87 / -57 | 121 | -57 (-0.7) | -90 (-4.4) | -215 / -91 | 16 / 24 |
| $10-20 | 113 | 23.5 / 39.8 | -19 (-0.7) | -55 (-2.2) | -70 / -38 | 133 | -18 (-1.2) | -31 (-2.6) | -23 / -20 | 9 / 12 |
| $20-100 | 191 | 21.4 / 27.7 | -10 (-2.3) | -13 (-3.9) | -14 / -11 | 189 | -2 (+0.1) | -13 (-3.4) | +13 / -2 | 6 / 8 |
| >$100 | 163 | 18.7 / 21.4 | -7 (-1.2) | -5.5 (-0.9) | **+10.7 / +10.7** (t 1.98; SPY +0.4) | 21 (6 days, small) | -3 | -7 | +41 / +13 | 5.5 / 7 |
| all | 709 | 18.7 / 37.8 | -29 (-6.5) | -66 (-6.4) | -90 / -26 | 609 | -40 (-7.3) | -60 (-8.1) | -8 / -34 | 12.5 / 20 |

\* The ON $1-5 hold mean is driven by a few multi-bagger name-days. With the top and bottom 2.5% trimmed it is -193,
and the median is -183. After the seat time only (ON_post), $1-5 rand hold is -166 mean.

**Under $5 (compare gross with net, not hit rates):** off-book arm entries have a 30-min gross of +2.6 bp against a net of
-108. Random minutes: gross -22, net -210. Hold to 15:50: gross -102 / -123, net -211 / -308. The +50 hit rate is under
10% while the reverse runs 75-80%. Big MFE here is just range, and a spread of about 1% eats it.

## Time windows (arm end30 net mean; random in brackets)
| window | OFF | ON | NOT_ON (OFF + seeded-not-seated) |
|---|---|---|---|
| 09:30-11:00 | -56 (-123) | -41 (-63) | -69 (-144) |
| 11:00-15:00 | -23 (-58) | -40 (-61) | -29 (-58) |
| 15:00-15:30 | -42 (-43) | -43 (-54) | -51 (-55) |

- **Small pockets that look good but do not hold up:**
  - OFF $20-100 arm 09:30-11:00: n=61, 36 name-days, 11 days. +50 36%, net -0.2, A/B +20 / -13.
  - ON $20-100 arm 09:30-11:00: n=125. Net +10 (t 0.8), A/B +38 / -5.
  - OFF >$100 random 15:00-15:30: net +11.6, A/B +26 / -6. Fails out of sample.
- **Buy at the first RTH minute after the mention:** the worst entry. OFF: end30 -88, hold -126 (09:30-11:00: -137 /
  -167). ON first-minute holds have a positive mean only because of outliers (median -48, trimmed -119).

## What kept each off-book name out (OFF, 709 name-days)
There are no ledgers before 9/16, so for 9/1-9/15 only the band status is known.
| blocker | name-days | tiers | arm end30 net | rand end30 net | rand hold net |
|---|---|---|---|---|---|
| pre-ledger, < $20 at mention | 198 | $1-5 127, $5-10 39, $10-20 32 | -77 | -151 | -226 |
| pre-ledger, > $100 | 46 | >$100 | -8 | -5 | +9 |
| pre-ledger, in band | 36 | $20-100 | -21 | -26 | -40 |
| thin_rvol, no band refusal | 223 | $20-100 108, $10-20 43, >$100 40, $5-10 32 | -21 (t -4.2) | -26 | -30 |
| thin_rvol + band | 41 | mixed | -22 | -68 | -89 |
| `red` (trending refusal) | 93 (3 days, small) | $10-100 | -12 | -20 | -29 |
| $100 cap only | 34 (2 days, small) | >$100 | -6 | -4 | +21 |
| no_tape / stale_tape_admit | 17 / 11 | mixed | +6 / -154 | -62 / -294 | -34 / -406 |
| silent (in band, no refusal logged) | 3 | $20-100 | too few | | |
| other / no refusal, out of band | 7 | | too few | | |

- **"Blocked only by the $20 floor" hardly exists.** On ledger days most sub-$20 momentum names are *seeded* and then
  refused at inclusion, so they land in SEED_ONLY. Top non-band reasons there: stale_tape_admit 85, no_tape 96. Only 3
  name-days had `below_min_price` as their only refusal. The nearest proxy is pre-ledger OFF names under $20 (198):
  strongly negative (random end30 -151, hold -226). The SEED_ONLY $1-5 name-days are also strongly negative
  (random end30 -140, hold -189).
- **The silent mom_open stale-price skip** explains almost none of the off-book set here: 3 name-days.
- **No blocker group has positive 30-min net with adequate n.** The positives are all small, few-day, or
  hold-to-close-only cells:
  - thin_rvol+band >$100 arm: +18.5, n=56, 14 name-days, but random is -2.4.
  - $100-cap-only hold: +21, 2 days.
  - no_tape >$100 hold: +47, 13 name-days.
  - SEED_ONLY thin_rvol $20-100 hold: +33 to +49, 27 name-days.

## Conclusions
1. Lowering the floor to $1 would add names whose median quoted spread is 80-125 bp (under $5) or 16-24 bp ($5-10).
   Their 30-min and hold-to-close outcomes are net negative, with day t down to -8, in both halves of the sample.
2. Off-book names in $20-$100 look like on-book names on 30-min runway (about -10 to -13 bp net on random minutes)
   and are not an untapped pool. On-book arm entries in $20-100 are about flat (-2); off-book ones are -10.
3. The only consistent positive is hold-to-close on >$100 off-book names. Random-minute hold is +10.7 net (gross +18.1
   vs SPY +0.4), t 1.98, A +12 / B +9. Arm hold is +10.3, t 1.35. This is outside the current $100 cap and is a hold
   effect, not intraday runway. Treat it as a lead to pre-register and re-test, not an edge.

## Data gaps and caveats
- Blocker attribution uses refusal *reasons logged anywhere that day* (any source or stage). It does not tell which gate
  fired first for the momentum path. There are no ledgers for 9/1-9/15.
- Seated = any shadow.jsonl row that day. Before 9/16 the book admitted sub-$20 names (ON has 75 name-days in $1-5 on
  9/1-9/15), so ON mixes rule regimes.
- The spread comes from 5 ten-second quote samples per name-day; the nearest sample is used. Real fills on thin names
  can be worse. Premarket is excluded (RTH only).
- Many cells were examined (3 entry types × 5 tiers × 3 windows × groups × blockers). Expect a few |t| around 2 by chance.
- Small-n cells are flagged SMALL in the raw output (n < 30, < 10 name-days, or < 5 days). ON >$100 has only 21
  name-days on 6 days.
- Tier uses the price at the first mention. Names that later crossed a tier boundary stay in their mention tier.
