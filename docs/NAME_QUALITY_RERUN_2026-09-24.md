# Name quality rerun + day-change fade check — 2026-09-24

**Market open; mini read-only** (study scripts via `/tmp` + `nice -n 15`). Bars from local SIP cache; today’s bars end ~16 min ago. No desk restart, no config edits.

## Plain English

We asked whether ranking the book by **how much a name is already up on the day** steers us into names that **spike then fade**. At the −50 cross, names up **about 5–8%** look best on both date halves (more “runners,” and on the later days better average returns). Names up **15%+** still often tag a quick +0.35R “runner,” but on the test half their **average** 30/60-minute return is sharply negative — they pop then give it back. **Volume pace** (SIP volume so far vs the stock’s own 20-day normal) helps into roughly **1.6–2.5×**; above **4×** the test half fades. **Strength vs SPY** does not add a clean ranking edge beyond day change (and Claude’s seed-time name-quality screen still does not beat the book). **Recommendation:** keep day change and volume pace in the runway score but **bend/cap** them (peak ~5–8% day change; volume pace lift into ~1.6–2.5, soft-cap ~4); do **not** weight strength vs SPY.

---

## 1. `name_quality.py` rerun

**Command (mini):** `nice -n 15 .venv/bin/python tools/studies/name_quality.py` with days  
`2026-09-16 … 2026-09-24` (9/24 included; SIP truncated ~16 min).

One 429 on a 9/23 fetch batch; retry filled. Scored **1,547** symbol-days (1,921 seen).

### Population

| Slice | n | up1 | r30 |
|---|---:|---:|---:|
| All names seen | 1547 | 50.2% | −0.030% ±0.028 |
| Admitted to book | 340 | 52.8% | +0.025% ±0.058 |
| Refused only | 1207 | 49.4% | −0.046% ±0.032 |

Halves: `{9/16,9/17,9/18,9/21}` \| `{9/22,9/23,9/24}`.

### By source (headline)

| Source | n | up1 | r30 |
|---|---:|---:|---:|
| momentum | 816 | 51.7% | −0.009% |
| trending | 526 | 48.5% | −0.027% |
| agy | 70 | 49.5% | −0.022% |
| movers | 68 | 49.1% | −0.117% |
| bb_live | 64 | 45.9% | −0.246% |

### Features vs up1 / r30 (stability = \|t\|≥2 and same sign both halves)

**vs up1 — STABLE:** `mins_open` (earlier better; t −2.7), `price` (higher better; t +2.4).  
**vs r30:** none STABLE. `price` closest (t +1.9).

**Strength vs SPY (`rs_spy`) — report explicitly:**

| Target | n | bottom third | top third | t (h1, h2) | Stable? |
|---|---:|---:|---:|---|---|
| up1 | 1444 | 50.4% | 50.0% | −0.2 (+0.9, −1.2) | **No** |
| r30 | 1547 | +0.003% | −0.112% | −1.4 (−1.0, −1.3) | **No** (hint: hotter RS → worse mean 30m) |

`move_open` (raw day change at seed) same story vs r30: hi −0.116% vs lo +0.004% (t −1.5), not stable.

### Held-out

Only **`mins_open`** cleared \|t\|≥2 on half 1. Ranked on half 2:

| Score third | n | up1 | r30 |
|---|---:|---:|---:|
| Bottom | 232 | 51.1% | −0.074% |
| Middle | 232 | 54.2% | +0.053% |
| Top | 233 | 49.8% | −0.072% |
| Admitted (book) | 156 | 52.8% | −0.001% |

**Headline:** seed-time features still do **not** beat the book; hottest relative-strength names are not better (slightly worse on r30).

---

## 2. Day-change fade at −50 crosses

**Script:** `tools/studies/day_change_fade_study.py` (scp → `/tmp`, import `mid_rise_runway_study` crosses + runner label).  
**Population:** supply-quality names when known, price $20–$100, RTH 09:35–15:30. Cache now includes 9/24 → **3,050** events (train n=**878** matches prior runway study; test larger with 9/23–24).  
**Train** ≤09-17, **test** ≥09-18.  
**rvol_pace** = SIP volume so far / (20-day avg daily SIP volume × `morning_funnel.expected_fraction`); available on 3,033/3,050 events.

### Day change % at the cross

| Bucket | TRAIN n | runner | ret30 | ret60 | −1% before +1% | TEST n | runner | ret30 | ret60 | −1% before +1% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ALL | 878 | 46.2% | −0.08% | −0.16% | 56.7% | 2172 | 47.5% | +0.07% | +0.12% | 44.8% |
| \<0 | 435 | 43.2% | −0.02% | −0.15% | 55.0% | 1090 | 46.4% | +0.13% | +0.18% | 41.6% |
| 0–3 | 260 | 48.5% | −0.11% | −0.14% | 55.6% | 789 | 44.6% | +0.00% | +0.02% | 49.3% |
| 3–5 | 89 | 44.9% | −0.27% | −0.18% | 62.5% | 158 | 48.1% | −0.09% | −0.07% | 50.6% |
| **5–8** | **67** | **50.7%** | **−0.00%** | **−0.23%** | **59.2%** | **81** | **65.4%** | **+0.54%** | **+0.66%** | **32.7%** |
| 8–15 | 21 | 61.9% | −0.41% | −0.45% | 69.2% | 41 | 80.5% | +0.21% | +0.67% | 52.6% |
| **15+** | **6** | **83.3%** | **+0.93%** | **+0.85%** | **33.3%** | **13** | **84.6%** | **−2.15%** | **−1.38%** | **30.8%** |

**Read:** runner rate rises with day change on **both** halves, but the **runner label rewards a quick spike** (0.35R or +0.6%). Mean path returns and fade% show **15%+ on test is a fade** (tiny n but large negative means). **5–8%** is the band with both-half runner lift and the cleanest test-half mean returns / low fade. **8–15** is mixed (high runner, train means negative + high fade).

### Volume pace (`rvol_pace`)

| Bucket | TRAIN n | runner | ret30 | ret60 | TEST n | runner | ret30 | ret60 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| \<0.8 | 345 | 37.7% | −0.12% | −0.22% | 917 | 41.5% | +0.05% | +0.06% |
| 0.8–1.2 | 232 | 41.8% | −0.15% | −0.24% | 653 | 47.5% | +0.06% | +0.11% |
| 1.2–1.64 | 147 | 51.0% | −0.05% | −0.20% | 308 | 53.2% | +0.13% | +0.26% |
| **1.64–2.5** | **75** | **65.3%** | **+0.12%** | **+0.23%** | **139** | **66.2%** | **+0.23%** | **+0.46%** |
| 2.5–4 | 53 | 71.7% | +0.09% | +0.06% | 62 | 59.7% | +0.13% | +0.11% |
| **4+** | **26** | **65.4%** | **+0.11%** | **+0.06%** | **76** | **57.9%** | **−0.10%** | **−0.24%** |

Both halves: lift from cold volume into **1.64–2.5**. Test **4+** mean returns turn negative → bend/cap, don’t linear-reward extreme pace.

### Strength vs SPY at the cross (`rs_spy`)

SPY missing for early-Sept train cache days (183 train events without RS). Buckets:

| Bucket | TRAIN runner (n) | TEST runner (n) | TEST ret30 |
|---|---|---|---|
| \<0 | 41.2% (393) | 46.8% (1108) | +0.12% |
| 0–1 | 31.2% (96) | 37.3% (389) | −0.05% |
| 1–3 | 52.9% (87) | 52.9% (397) | +0.07% |
| 3–5 | 55.6% (45) | 44.7% (152) | −0.09% |
| 5–8 | 48.9% (47) | 64.5% (76) | +0.57% |
| 8+ | 70.4% (27) | 82.0% (50) | **−0.41%** |

No band with **both** strong runner lift **and** positive mean returns in both halves with healthy n. Largely tracks day change; name_quality already flat/negative on `rs_spy`.

---

## 3. Recommendation (evidence only)

| Feature | Call | Why |
|---|---|---|
| **Day change %** | **Bend / soft-cap** (rise into ~5–8%, flatten or fall by 15%+) | Both halves: higher day_chg → higher *runner*; test 15+ mean path **fades**. Linear `z(day_chg)` without a cap pushes the book toward that fade tail. Do **not** drop (5–8% and ≥5% still help). |
| **Volume pace** | **Keep, bend above ~4** | Both halves improve into **1.64–2.5**; test **4+** mean returns negative. |
| **Strength vs SPY** | **Drop / no weight** | name_quality not stable; cross buckets no both-half sweet spot; collinear with day_chg. **No evidence** it earns its own term. |

*Measured: tables above. Inferred: exact score kink points (~8% / ~4×). Branch `runway-study`; not pushed; mini repo untouched.*
