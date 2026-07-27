# Relative Strength screener

`rs_screener.py` — an IBD-style RS Rating for the full tradable US universe,
computed from split-adjusted daily closes against SPY, refreshed once a day
after the close.

The desk already tracks two kinds of name: **momentum** (Discord mentions plus
the three-indicator engine) and **trending** (Stocktwits heat). Both answer
"what is hot right now". This answers a different question — "what has actually
been beating the market for months" — which is what you want before deciding
which hot name deserves the capital.

---

## Quick start

```bash
# First run backfills ~275 sessions for ~13k symbols. Run it after the close.
.venv/bin/python rs_screener.py --once

# Every field for specific names, including ones the filters dropped.
.venv/bin/python rs_screener.py --explain NVDA AAPL SPY

# Drop the cache and start over (needed after changing rs_bar_adjustment).
.venv/bin/python rs_screener.py --rebuild
```

Enable the scheduled process with `"rs_screener_enabled": true` in
`config/bot_config.json`; `start_all.py` then launches it alongside the swing
screener. Output lands in `rs_ratings.json` and is served at `/api/rs` and in
the `rs` slice of `/api/state`.

---

## The formula

```
rs_raw = 0.40·(P₀/P₆₃) + 0.20·(P₀/P₁₂₆) + 0.20·(P₀/P₁₈₉) + 0.20·(P₀/P₂₅₂)
```

`Pₙ` is the split-adjusted close *n* **benchmark sessions** back — not *n* rows
back; see "Same session, same tape" below. The four weights sum to 1.0, so a
stock that went nowhere scores exactly `1.0` and the raw number reads as
"×the market's starting point".

These are **trailing** windows all ending today, which is the IBD form.
`rs_form: "quarters"` switches to the non-overlapping reading
(`P₀/P₆₃`, `P₆₃/P₁₂₆`, `P₁₂₆/P₁₈₉`, `P₁₈₉/P₂₅₂`). The two are genuinely different
statistics that produce different rankings, so `rs_form` is stamped on every row
— without it, two runs are silently incomparable.

**`rs_rating`** is then the cross-sectional percentile of `rs_raw`, mapped to
`1..99`:

```
rs_rating = clip(ceil(99 · rank_pct), 1, 99)
```

Ties share a rating. Note this is **99 buckets, not 100** — the top bucket is
the top ~1/99 of the population, which is 102 names out of 10,000, not 100. The
common shorthand "RS 99 means top 1%" is slightly wrong.

Alongside, for transparency: plain `ret_1m/3m/6m/12m` over 21/63/126/252
sessions, and per-window `rs_vs_spy = (1 + r_stock) / (1 + r_spy)`.

**This is an approximation.** IBD's actual RS Rating formula is proprietary.
Expect the ordering of well-known leaders to be recognisable and the exact
numbers not to match.

---

## Four things this module exists to get right

### 1. Alpaca serves RAW bars by default

`StockBarsRequest.adjustment` defaults to `None`, which the API treats as `raw`.
On a 252-session lookback every stock split becomes a fake ±% move: a 4:1 split
reads as −75% and buries a leader at RS 1.

Measured against live data — the first 300 tradable symbols, 400 days, pulled
both ways — **7 of 300 (2.3%)** had a single-day raw gap above 1.8× that split
adjustment removed:

| Symbol | Raw one-day gap | Split-adjusted |
|---|---|---|
| GVH | 189.5× | 3.96× |
| ADV | 28.5× | 1.30× |
| DXST | 28.5× | 3.24× |
| AGL | 24.2× | 2.18× |

Across ~13,290 symbols that is on the order of 300 names with a wildly wrong
12-month return — and they land at the extremes of the percentile, i.e. at the
top of the ranked output. So `rs_bar_adjustment` defaults to `split` and `raw`
is refused outright rather than accepted and warned about.

**Why not `Adjustment.ALL`?** Dividend adjustment is arguably more correct, but
every dividend also restates all prior bars, so an `ALL` cache needs repair on
every ex-div date across a large slice of the universe — a permanent daily
re-full. `SPLIT` restates only on splits, a handful a day. IBD measures price
performance anyway.

**Consequence:** these are **price returns, not total returns.** A 4%-yielder's
real 12-month total return is about 4pp above what is reported here.

### 2. A percentile only means something over the population it ranked

The screener ranks the **whole bar-covered universe first**, then applies the
day-trading filters. Ranking the survivors of a strength screen would make
"RS 90" mean "top 10% of names that already look strong" — a much weaker claim
than "top 10% of the market", and indistinguishable from it on the screen.

Every row therefore carries `population`, `as_of`, `rs_form`, `adjustment` and
`feed`. `population` alone would not tell you the number was computed off
Friday's tape with the trailing form.

Two rules protect this:

- **A fetch failure must not silently shrink the population.** Symbols whose
  batch errored are recorded as unreachable and excluded from the ranking. If
  more than `rs_max_stale_frac` of the universe is lost, the run **refuses to
  publish and leaves the previous file in place** — overwriting a sound
  percentile with one computed over a truncated population is worse than serving
  yesterday's, because the new file looks identical and means something else.
- **Every symbol's P₀ must be the same session.** A name whose last real bar
  lags `as_of` by more than `rs_max_p0_staleness_sessions` is excluded, not
  given a stale rating.

### 3. Same session, same tape

`closes.iloc[-63]` is 63 *rows* back, not 63 sessions — for a symbol with
missing bars that silently compares its 61st prior session to SPY's 63rd. Every
symbol is reindexed onto **SPY's** session calendar before anything is measured.

Two distinct guards, because they answer different questions:

- **Coverage** — the fraction of benchmark sessions carrying a real bar. Below
  `rs_min_coverage`, the symbol is dropped.
- **Anchor presence** — coverage is *not sufficient*. A name that IPO'd 200
  sessions ago has 200/252 = 79% coverage but simply has no P₂₅₂. Each anchor is
  required individually, and when one is missing `rs_raw` is `None`. The weights
  are **never renormalised** over the surviving terms: a 3-term blend is a
  different statistic and ranking it against 4-term ones repeats the error in §2.

Session dates are keyed in **Eastern time**, not UTC. Alpaca stamps daily bars at
04:00 UTC in EDT and 05:00 UTC in EST, so a UTC key files every summer bar a day
early and shifts every anchor.

### 4. Never invent a value

Missing history yields `None`, never `0.0` and never `1.0`. `insufficient` on
each row names exactly what could not be computed (`["ret_12m", "sma200"]`), so
"this stock is not above its 200-day average" stays distinguishable from "this
stock has not existed for 200 days".

Watch `signals.calc_rvol` in particular: it returns a constant `1.0` Series below
20 bars — a manufactured "perfectly average volume" — which `trailing_stats`
converts to `None`.

---

## The bar cache

`rs_cache.sqlite` (gitignored, rebuildable). SQLite rather than parquet because
`pyarrow` is a ~50 MB dependency that is not installed, this repo has no other
binary caches, and `sqlite3` is stdlib.

Measured on a 1,505-symbol slice of the real universe and extrapolated:

| | 1,505 symbols | ~13,291 symbols (projected) |
|---|---|---|
| Cold run (full backfill) | 22 s, 63 pages | ~560 pages → **6–10 min**, throttle-bound |
| Warm run (daily) | 8 s, 7 pages | ~45 requests → **~1 min** |
| Cache after a cold run | 1,502 symbols, 323k bars | ~2.9M bars, ~250 MB |
| Symbols with no IEX history at all | 55 (3.7%) | marked and not re-requested |

The cold run is bound by the 100/min throttle, not by bandwidth. The warm run is
bound by pandas, not the network: aligning and building rows for 1,423 symbols
takes 2.7 s, so ~25 s for the full universe.

The cache is not an optimisation — it is what makes a daily job viable at all.

**Only settled sessions are stored.** Alpaca serves a *partial* bar for a session
in progress, and caching it means the next refresh compares that partial close
against a later partial close of the same day, exceeds the split tolerance, and
"repairs" a symbol that never had a corporate action. Observed live before the
guard: **78 spurious repairs** across a 1,500-name universe from two runs twenty
minutes apart. `last_storable_session()` keeps the live session out of the store,
not merely out of the calendar.

**Symbols with no history are remembered.** About 1–4% of the tradable list —
ADRs and thin foreign listings — has no IEX daily history at all. Without a
marker they look like brand-new listings on every run and the full backfill
window is re-requested forever; the bars never arrive, only the request bill
does. `mark_empty()` / `recently_empty()` record the answer for under a day, so a
genuinely new listing is still picked up on the next scheduled run.

**Split repair.** Every incremental refresh deliberately re-requests the last
`rs_overlap_sessions` (5) sessions it already holds. If those closes come back
different by more than `rs_split_tolerance` (0.5%), the vendor restated history
and the symbol is purged and refetched in full. The overlap *is* the detector.
0.5% sits comfortably between the smallest common split (3:2, +50%) and a
routine late-print revision of a fraction of a cent, and on a $0.30 stock it is
still below one tick.

The adjustment factor is never applied locally — re-implementing the vendor's
arithmetic will drift from it; purge-and-refetch cannot. Repairs are logged to
`symbol_meta.repairs` / `last_repair_ratio` for audit:

```sql
SELECT symbol, repairs, last_repair_ratio FROM symbol_meta WHERE repairs > 0;
```

Ratios should cluster near clean fractions (0.5, 0.1, 2.0, 3.0). Scattered
values mean the tolerance is too tight and the detector is chasing bar revisions.

---

## Free-tier limitations

- **IEX, not consolidated.** `alpaca_api._get_feed_arg` is always IEX, so
  `avg_vol_50d` is IEX volume — a fraction of the real tape. Calibrate
  `rs_min_avg_vol_50d` against IEX, not against what you would read on Finviz.
  `tools/morning_funnel.py:196-210` has the measured error table for the same
  family of problem.
- **Price returns only** — dividends are excluded (see §1).
- **The population includes ETPs.** Alpaca's `Asset` model has no ETF flag, so
  the ranked universe contains ETFs, leveraged ETPs, ADRs, preferreds, units and
  rights alongside common stock. In a strong tape the top of an unfiltered RS
  list fills with the TQQQ/SOXL family. `rs_exclude_etp` (default on) strips them
  from the **served list**, never from the ranking, using a Finnhub
  `stock/profile2` lookup on the top `rs_limit` names only. It is conservative:
  a name is dropped only when Finnhub affirmatively answers and reports no
  industry. Without a `finnhub_key` nothing is stripped and the run says so in
  `degraded[]`.
- **The rate limit is shared.** Alpaca's 200 req/min is per *account*, and
  `signal_engine.py`, `dashboard.py` and `trade_bridge` use the same key.
  `rs_max_req_per_min` defaults to 100 so a backfill cannot starve the live
  engine, and a one-shot run refuses to start during market hours without
  `--force`.

---

## What this is not for

RS is a **daily** number computed off completed sessions. It does not move
intraday and it must not be treated as though it does.

In particular it does **not** belong in the momentum desk's `row_rank()`.
`docs/MONITOR_ROADMAP.md:117-120` allows exactly one ranking function, and
feeding a once-a-day figure into a two-second intraday sort is the mistake the
Phase 7 cut warns about at line 769. When RS earns a place on the desk it should
be a **dated context column** — never an alert, never a sort term.

---

## Configuration

All keys live in `config.py :: DEFAULT_CONFIG` with inline comments, and all are
in `SAFE_CONFIG_KEYS` so the dashboard config panel can edit them.

| Key | Default | Notes |
|---|---|---|
| `rs_screener_enabled` | `false` | `start_all.py` launch gate |
| `rs_universe_source` | `"alpaca"` | `alpaca` \| `finviz` \| `file` |
| `rs_bar_adjustment` | `"split"` | `raw` is refused |
| `rs_backfill_calendar_days` | `400` | ≈275 sessions |
| `rs_lookback_sessions` | `252` | the 12-month anchor |
| `rs_overlap_sessions` | `5` | the split detector |
| `rs_split_tolerance` | `0.005` | |
| `rs_min_coverage` | `0.80` | fraction of benchmark sessions with a real bar |
| `rs_min_population` | `500` | below this, no ratings at all |
| `rs_max_stale_frac` | `0.10` | above this, refuse to publish |
| `rs_form` | `"trailing"` | `trailing` \| `quarters` |
| `rs_exclude_etp` | `true` | strips the served list only |
| `rs_max_req_per_min` | `100` | of Alpaca's 200 — the key is shared |
| `rs_run_times` | `["18:30"]` | once a day; RS is a 12-month statistic |
| `rs_limit` | `100` | rows kept in the file |
| `rs_min_rs_rating` | `80` | filters below here are applied AFTER ranking |
| `rs_min_price` | `10.0` | |
| `rs_min_avg_vol_50d` | `500000` | IEX volume |
| `rs_require_above_sma50` | `true` | |
| `rs_require_above_sma200` | `false` | |
| `rs_use_rvol_filter` / `rs_min_rvol` | `false` / `1.5` | |
| `rs_use_adr_filter` / `rs_min_adr_pct` | `false` / `3.0` | |

Optional filters use a `use_X`/`min_X` pair rather than a `null` sentinel,
because the dashboard config panel round-trips an empty numeric input as `""`,
not `null` — a nullable knob would silently disable itself.

`rs_universe.json` at the repo root, if present, overrides the universe entirely.

---

## The Finviz adapter

`finviz_universe.py`, off by default. Read its module docstring before enabling
it. Everything the Finviz screener filters on is already computable from the bars
this module has to pull anyway; the one thing it buys is a stocks-only universe.
Against that: Finviz's free tier has no CSV export so it means HTML scraping,
their terms of service do not permit it, they sit behind Cloudflare, and — most
importantly — pre-filtering the universe narrows the ranking population and
changes what an RS rating means.

Every failure path returns `[]` and the screener falls back to the Alpaca
universe. A broken scrape never ends a run.

---

## Verifying a run

The single best end-to-end check:

```bash
.venv/bin/python rs_screener.py --explain SPY
```

**SPY compared against SPY must show `1.000x` in all four windows.** It is true
by construction, so if it is not, the calendar alignment is wrong and no other
number in the output can be trusted.

Then:

1. Run `--once` twice. The second run should take seconds, not minutes — that is
   the cache proving itself.
2. `SELECT adjustment, count(*) FROM symbol_meta GROUP BY 1` should show only
   `split`.
3. Spot-check the top 20 against a free public source. Exact agreement is not
   expected; recognisable ordering is.

Tests: `tests/test_rs_core.py`, `tests/test_rs_cache.py`,
`tests/test_rs_screener.py`, `tests/test_finviz_universe.py`.
