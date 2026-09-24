# Runway study — 2026-09-24

**Question:** Keep fast %R crossing −50 as the *trigger*, but which names
actually have room to run? Today's four trail exits all had MFE ≤ 0.09R.

**Method (ledger + local bars only until 16:05 ET — no live Alpaca/Finnhub calls):**
- Bars: `ai_reports/runway_bars_cache.pkl` (SIP 1m already on disk; through 2026-09-23).
- Supply: Movers / Trending / Research first-seen from admit ledgers
  (`supply_first_seen.json`, days 2026-09-16…24 dense; earlier days only if
  the name appears in the bar cache without a supply row → tagged `source=?`).
- Event: each upward cross of smoothed fast %R through −50 (length 21, EWM 7,
  matching desk RTE fast), RTH 09:35–15:30, price $20–$100, after the name
  first appears in a quality source when known.
- Label **runner** = within 60 minutes, MFE ≥ 0.35R **or** +0.6% price, *before*
  price hits −1R (stop = outcome stop % if known, else 1.5%).
- Split: **train** days ≤ 2026-09-17, **test** ≥ 2026-09-18.
- Script: `tools/studies/mid_rise_runway_study.py` →
  `ai_reports/mid_rise_runway_study.json`.

**Not the same as fill MFE.** Outcome `mfe_r` is censored by our trail/stop.
This study scores the *path after the cross* from bars whether or not we held.

---

## Event counts (measured)

| Set | n | Runner rate | Mean MFE_R (60m) |
|---|---:|---:|---:|
| All events | **2243** | — | — |
| Train (≤09-17) | **878** | **46.0%** | 0.35 |
| Test (≥09-18) | **1365** | **49.5%** | 0.47 |

Skipped (measured): price band 5966, outside RTH 868, before source sighting 306,
not in supply file 274. Sep-24 not in bar cache yet (no API until after 16:05).

For comparison: **filled** outcomes since 9/1 with quality sources had only
**~4%** with hold-censored MFE ≥ 0.35R (n=389) — we are selecting poorly among
crosses that, as a class, often *do* have runway.

---

## Features — train and test

Direction that **helps on both halves** (measured):

| Feature | What "good" looks like | Train | Test | Notes |
|---|---|---|---|---|
| **mins_open &lt; 90** | Cross in first 1.5h | 59.9% runner (n=142) | 60.3% (n=239) | Lift ≈ +14 / +11 pp vs base |
| **day_chg ≥ +5%** | Already a real up-day | 57.4% (n=94) | 68.8% (n=80) | Lift ≈ +11 / +19 pp; keeps ~6–11% |
| **day_chg ≥ +3%** | Milder up-day | 53.0% (n=183) | 57.0% (n=186) | Lift ≈ +7 / +7 pp; keeps ~14–21% |
| **used_range high** (top tercile) | Day already traveled | 58.8% (n=296) | 63.3% (n=267) | Momentum day, not flat |
| **ema9 slope high** | Short trend steep | 58.7% (n=293) | 62.7% (n=346) | Continuous rank signal |
| **far from HOD** (bottom tercile of dist_hod) | Cross while **not** hugging the high | 56.5% (n=292) | 62.3% (n=377) | **Room above** matters |
| **far below 30m swing** | Pullback into −50 | 60.6% (n=292) | 62.9% (n=402) | Same idea |

**Hurts or flat (measured):**

| Feature | Finding |
|---|---|
| Near HOD (`dist_hod > −1%`) | Runner **drops** (train 35%, test 46%) — chasing the high |
| above_vwap alone | Tiny lift (+0 / +3 pp) |
| ema9&gt;ema20 alone | Flat / slight negative |
| rvol_proxy (1m vs session median) | Flat — not the movers SIP pace |
| slow %R rising at cross | **n=3 / 7** — study's 15m slow line almost never flags; **do not** use this as evidence for/against the live slow-rising latch |
| Research (agy/xai) | Mixed; xai train weak (26%, n=31), test ok (56%, n=73) |
| movers (test only dense) | **78%** runner (n=32) — promising but thin |

Time-of-day: 09:30–10:30 buckets are the hot ones on both halves; early afternoon
is colder.

---

## Simple rules (1–2 features)

Keep enough volume for ~1 open / 10 min on a ~10-seat book → prefer rules that
keep **≳15%** of crosses unless the lift is huge.

| Rule | Keep (train→test) | Runner train→test | Both-half lift? |
|---|---|---|---|
| `mins_open < 90` | 16% → 18% | 60% → 60% | **Yes** |
| `day_chg ≥ 3` | 21% → 14% | 53% → 57% | **Yes** |
| `day_chg ≥ 5` | 11% → 6% | 57% → 69% | Yes, but thin for cadence |
| `above_vwap & ema9>ema20` | 35% → 36% | 46% → 53% | Weak |
| Near HOD + vwap | — | Worse than base | **No** |

**Recommended hard gate (at most one):** none mandatory tonight.
If one is required for simplicity: **`day_chg_pct ≥ 3` at cross** (stable lift,
still ~1/5–1/7 of crosses). Morning preference is better as **rank boost** than
a hard "no trades after 11:00" (would starve the afternoon goal).

---

## Recommended ranking score (book server)

Score names on the book (higher = open first when several are near −50):

```
runway_score ≈
    1.5 * z(day_chg_pct)
  + 1.0 * z(used_range_pct)
  + 1.0 * z(ema9_slope)
  + 1.0 * z(-dist_hod_pct)      # farther below HOD = better
  + 0.5 * z(-dist_swing30_pct)  # deeper pullback = better
  + 0.5 * (mins_open < 90)
  + 0.5 * (source == movers)
```

Then combine with proximity to the arm:

```
seat_priority = runway_score + w * closeness_to_cross
```

where `closeness_to_cross` rises as fast %R approaches −50 from below
(already the book-server idea).

**Drop / do not gate on:** near-HOD chase, VWAP alone, raw ema9>ema20 alone,
study `rvol_proxy`, research-only boost without movers/trending confirmation.

**Slow %R rising:** keep measuring live (mid_rise latch); this offline approx
did not produce enough `slow_rising=1` events to judge. Separate whatif later.

---

## Expected effect on open cadence (inferred)

- Raw −50 crosses in this cache ≈ **150 / day** among cached supply names
  (2243 / 15 days) — far more than we can trade.
- Keeping top ~20% by `runway_score` still leaves ~30 candidates/day before
  seat limits → compatible with **≥1 open / 10 min** if the book stays warm.
- Hard `day_chg ≥ 5` alone may be **too tight** for afternoon cadence (6–11% keep).
- Today's tiny MFEs look like **selection**, not "−50 never runs": the cross
  population's median path MFE_R is ~0.35–0.47.

---

## After 16:05 (optional extension)

1. Fetch SIP 1m for 2026-09-24 supply names + any missing train days; append cache.
2. Re-run script; confirm movers lift with larger n.
3. Rebuild slow line with the engine's true 15m %R if needed for the RSI-direction
   question.

---

## Measured vs inferred

| Claim | Status |
|---|---|
| Event counts, runner rates, feature tables, rule lifts | **Measured** (local bars + ledgers) |
| Fast %R params match live RTE fast | **Measured** (config 21 / 7) |
| Slow %R rising feature | **Weak / possibly wrong approx** — not decisive |
| Ranking weights | **Inferred** from tercile lifts (not fit by ML) |
| Cadence math (~30 candidates/day after top quintile) | **Inferred** from cache density |
| "Today's bad MFE was selection not trigger" | **Inferred** (cross base rate ≫ fill MFE rate) |

*Branch: `runway-study`. Not pushed. Mini not modified.*
