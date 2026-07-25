# Momentum Monitor — Improvement Roadmap

Target: `momentum-monitor/momentum_signal.py` and its supporting modules.

This document is written to be handed to Claude Code one ticket at a time. Each
ticket is self-contained: goal, files, data available today, config flag,
acceptance criteria, and tests. Work phases in order — later phases depend on
earlier ones.

---

## Ground rules for every ticket

1. **Additive and feature-flagged.** Every behavioral change is gated by a key in
   `momentum-monitor/momentum_config.json`. The default value must reproduce
   today's behavior exactly. Jonathan trades on this screen every morning; a
   fresh checkout with no config edits must look and behave identically to today.
2. **Two places define config.** `DEFAULTS` in `momentum_signal.py` *and*
   `momentum_config.json`. Add the key to `DEFAULTS` (this is the real default);
   only add it to the JSON file when the shipped value differs from the default.
3. **The monitor is a renderer, not a data source.** It polls
   `GET {dashboard}/api/state` every `poll_interval` (2.0s) and the Stocktwits
   API every `stocktwits_poll` (60s). Prefer adding fields server-side in
   `dashboard.py` / `signal_engine.py` over adding network calls to the monitor
   loop. Any network call inside `main()`'s `while True` will stall the render.
4. **Never let a new feature crash the loop.** The existing code swallows
   exceptions aggressively (`except Exception:  # noqa: BLE001`) on purpose.
   Match that. A broken sparkline must degrade to `—`, not kill the desk.
5. **Tests** live in `tests/` (pytest, `testpaths = tests`). Monitor tests follow
   `tests/test_rsi_focus_column.py`: `sys.path.insert(0, ".../momentum-monitor")`
   then import pure functions. Keep new logic in pure functions so it is testable
   without a live feed.
6. **Lint:** ruff, `line-length = 120`.

---

## What the feed already gives you

Read this before starting. Several tickets are smaller than they look because
the data is already on the wire and simply is not rendered.

### Top level of each row in `state["tickers"]`
Built in `dashboard.py :: _snapshot()` (line ~1203).

| Field | Source | Notes |
|---|---|---|
| `ticker`, `price`, `day_open`, `pct_change` | price loop | |
| `mention_count`, `mention_window`, `mention_burst` | Discord OCR | daily / in-window / burst flag |
| `find_it_first` | mention tracker | TTL-limited |
| `sentiment` | `_ticker_sentiment()` | `{count, ...}` |
| `confluence` | `_confluence_sources()` | `{sources: [...], count: N}`, sources ⊂ `{scanner, chat, alert, squeeze}` |
| **`rvol`**, **`day_vol`** | `_vol_loop()` via yfinance | **already present, never rendered — see T2.1, the value is wrong intraday** |
| `funnel` | `_funnel_loop()` | `{score, state, rvol, rejects}` — only for tickers the funnel ranked |
| `signal_proximity` | `signal_engine.py` | merged from `signal_state.json` |

### `signal_proximity` in `three_indicator` mode
From `signal_engine.py :: three_indicator_state()`. This is the active strategy.

`strategy`, `pinned`, `price`, **`proximity_pct`** (0–100 buy completion),
`status` (`watching` / `aligning` / `buy_zone` / `in_position` / `exit_signal`),
`in_position`, `buy_price`, `is_hot`, **`mention_velocity`**, `bars_fetched`,
`data_source`, `cm_rsi`, `cm_ok`, `cm_rsi_rising`, `pctr`, `pctr_ok`,
`pctr_rising`, `pctr_slow`, `pctr_falling`, `pctr_slow_falling`,
`pctr_deep_os`, `macd_cross`, `macd_sep_ratio`, `macd_ok`, `buy_signal`,
`sell_signal`.

**`proximity_pct` and `mention_velocity` already exist.** T1.2 and T1.3 are
mostly rendering work.

### Stocktwits rows
From `momentum-monitor/stocktwits_trending.py :: display_rows()`:
`rank`, `symbol`, `price`, `pct_change`, `volume`, `avg_vol`, `high_52w`,
`low_52w`, `market_cap`, `trending_score`, `look`, `look_reason`.

### Session clock
`session_clock.py` exports `ET`, `WINDOWS`, `SHOTS`, `session_window()`,
`next_shot()`, `session_line()`. `session_line()` is already in the monitor
header. `session_window()` returns `(label, guidance, color)`. `WINDOWS` encodes
the tranche schedule: pre-market scan → T1/T2/T3 funnel+entry → wind-down →
exits only.

---

## Phases

| Phase | Theme | Tickets |
|---|---|---|
| 0 | Foundation | T0.1, T0.2 |
| 1 | Zero-new-data quick wins | T1.1, T1.2, T1.3 |
| 2 | Volume truth | T2.1, T2.2 |
| 3 | Shape | T3.1 |
| 4 | Memory | T4.1, T4.2 |
| 5 | Signal quality | T5.1, T5.2, T5.3 |
| 6 | Context | T6.1 |
| 7 | Fundamentals | T7.1, T7.2 |

**Design note resolving a conflict:** T1.2 (sort by distance-to-trigger) and T5.2
(sort by confluence) both want to reorder rows. Do **not** implement two sorts.
T1.2 builds a single `row_rank()` function; T5.2 adds a weighted term to it.
There is exactly one ranking function in the final design.

---

# Phase 0 — Foundation

## T0.1 — Ranking and render seam

**Goal.** Before adding columns, carve out the seams the later tickets plug into,
with zero behavior change.

**Files.** `momentum-monitor/momentum_signal.py`

**Work.**
1. Extract row ordering out of `Feed.ingest()` into a module-level pure function:
   ```python
   def row_rank(row: dict, first_seen: float, now: float,
                server_idx: int, cfg: dict) -> tuple
   ```
   It must reproduce today's ordering exactly: newest `first_seen` first for
   `new_ttl` seconds, then server mention-rank order as the tiebreak.
   `Feed.ingest()` calls it.
2. `momentum_table()` currently hardcodes its column list. Refactor to build
   columns from an ordered spec list so later tickets append columns without
   re-editing the loop body. Column set and order must be unchanged.

**Config.** None.

**Acceptance.**
- `momentum_table()` renders byte-identical output for a fixed fixture row set
  before and after.
- Ordering unchanged for a fixture with mixed `first_seen` ages.

**Tests.** `tests/test_monitor_ranking.py` — `row_rank()` puts a symbol first
seen 10s ago above one first seen 300s ago with `new_ttl=120`; past the TTL,
server order wins.

---

## T0.2 — `SymbolHistory` ring buffer

**Goal.** A bounded, in-memory per-symbol time series. Sparklines (T3.1), mention
velocity smoothing (T1.3), and the journal (T4.1) all need it. Build it once.

**Files.** New — `momentum-monitor/symbol_history.py`

**Work.**
```python
class SymbolHistory:
    """Bounded per-symbol sample ring. Memory-only; nothing persists here."""
    def __init__(self, maxlen: int = 120): ...
    def push(self, sym: str, ts: float, *, price=None, mention_window=None,
             proximity_pct=None, rvol=None) -> None: ...
    def series(self, sym: str, field: str) -> list[float]: ...  # oldest → newest
    def age(self, sym: str) -> float | None: ...   # seconds spanned
    def drop(self, sym: str) -> None: ...
    def prune(self, live: set[str]) -> None: ...   # evict symbols off the feed
```
- `collections.deque(maxlen=...)` per symbol. At `poll_interval=2.0`, 120 samples
  ≈ 4 minutes of tape. Make `maxlen` derive from a config key, not a constant.
- `prune()` is called once per loop with the current symbol set so a long session
  does not leak memory on churned symbols.
- Call `history.push(...)` for every row in `main()`'s loop, right after
  `feed.ingest(...)`. Nothing reads it yet.

**Config.** `"history_samples": 120`

**Acceptance.**
- Ring never exceeds `maxlen`; oldest sample is evicted first.
- `series()` returns oldest→newest and skips `None` samples.
- `prune()` removes symbols absent from the live set.
- No visible change on screen.

**Tests.** `tests/test_symbol_history.py` — bounded length, ordering, `None`
handling, prune eviction.

---

# Phase 1 — Zero-new-data quick wins

## T1.1 — Setup age timer

**Goal.** When `FOCUS` lights up, show how long it has been lit. A setup that
fired 12 seconds ago and one that has been sitting 6 minutes are different
trades.

**Files.** `momentum_signal.py` (`Feed`, `_rsi_focus_cell`, `momentum_table`)

**Work.**
1. `Feed` tracks a rising edge per symbol: `self.focus_since: dict[str, float]`.
   Set on the transition *into* FOCUS, clear on the transition out. The FOCUS
   predicate is the existing `rsi_focus_trigger(row, ...) -> (rsi, hit)`.
2. Add a pure formatter:
   ```python
   def _focus_age_str(seconds: float | None) -> str   # "0:14", "6:02", "" if None
   ```
   Cap the display at `9:59+` so the column cannot widen unboundedly.
3. `_rsi_focus_cell()` takes an optional `age` and renders
   `FOCUS 0:14  3·−99/−77`. Non-FOCUS rows unchanged.
4. Color the age: green under `focus_age_fresh_sec`, yellow under
   `focus_age_stale_sec`, dim beyond. A stale FOCUS should look stale.

**Config.**
```json
"focus_age_enabled": true,
"focus_age_fresh_sec": 60.0,
"focus_age_stale_sec": 180.0
```
`focus_age_enabled` defaults `true` — this is additive text inside an existing
cell and does not move anything. If it disturbs the layout, default it `false`.

**Acceptance.**
- Age starts at `0:00` on the FOCUS rising edge.
- FOCUS dropping and re-firing resets the timer (does not resume).
- A symbol leaving and rejoining the feed resets the timer.
- Flag off ⇒ cell identical to today.

**Tests.** `tests/test_focus_age.py` — rising edge sets, falling edge clears,
re-fire resets, formatter output for 0 / 14 / 62 / 3600 seconds.

---

## T1.2 — Distance-to-trigger

**Goal.** Today the Setup column is binary: `FOCUS` or dim. You cannot see who is
*about to* fire. Turn it into a queue.

**Files.** `momentum_signal.py`

**Data.** No new fields needed. `signal_proximity.proximity_pct` is already the
engine's own 0–100 buy completion. The FOCUS legs are `cm_rsi` vs
`rsi_focus_max`, and `pctr`/`pctr_slow` vs `[pctr_focus_lo, pctr_focus_hi]`.

**Work.**
1. Pure scorer:
   ```python
   def setup_distance(row: dict, rsi_max: float,
                      pctr_lo: float, pctr_hi: float) -> float | None
   ```
   Returns 0.0 = firing now, 1.0 = far away, `None` = untracked/pending. Combine
   the normalized gap on each leg (how far `cm_rsi` is above `rsi_max`; how far
   the worse of `pctr`/`pctr_slow` is outside the band). Blend with
   `proximity_pct` — do not ignore the engine's own number.
2. Render the shortfall in the Setup column for near-miss rows only
   (`setup_distance` below `setup_near_threshold`), e.g.
   `NEAR  rsi 38→35  %R −72→−75`. Rows further out keep today's dim readout.
3. Feed the score into `row_rank()` from T0.1 as a **lower-priority term than
   the existing new/mention ordering**, gated by `setup_sort_enabled`
   (default `false`). Do not change the default sort in this ticket — ship the
   column first, let it be watched for a few sessions, then flip the sort.

**Config.**
```json
"setup_distance_enabled": true,
"setup_near_threshold": 0.25,
"setup_sort_enabled": false
```

**Acceptance.**
- A row satisfying both legs scores `0.0` and still renders `FOCUS`.
- A row with `cm_rsi` just above `rsi_focus_max` scores near 0 and shows `NEAR`.
- `signal_proximity` missing ⇒ `None`, cell renders `—` as today.
- `setup_sort_enabled: false` ⇒ ordering byte-identical to T0.1.

**Tests.** `tests/test_setup_distance.py` — monotonicity (as `cm_rsi` falls
toward the threshold the score decreases), boundary at exactly `rsi_max`, `None`
paths, and that a firing row scores below any near-miss row.

---

## T1.3 — Mention velocity

**Goal.** `12/47` does not say whether mentions are accelerating or dead. The
derivative is the signal.

**Files.** `momentum_signal.py`

**Data.** `signal_proximity.mention_velocity` already exists — but it is only
populated for tickers the engine is actively tracking. `mention_window` is on
every row. Use the engine value when present, otherwise derive from the
`mention_window` series in `SymbolHistory` (T0.2).

**Work.**
1. ```python
   def mention_trend(hist_series: list[float],
                     engine_velocity: float | None,
                     rise: float, fall: float) -> str   # "↑↑" | "↑" | "→" | "↓" | ""
   ```
   Compare the recent half of the window against the older half. Require at
   least `mention_trend_min_samples` samples before emitting anything other than
   `""` — an arrow computed off two data points is noise, not information.
2. Append the arrow to the existing Mentions cell: `12/47 ↑↑`. Color it
   (green rising / dim flat / red falling).

**Config.**
```json
"mention_trend_enabled": true,
"mention_trend_min_samples": 8,
"mention_trend_rise": 1.5,
"mention_trend_fall": 0.6
```

**Acceptance.**
- Fewer than `min_samples` ⇒ empty string, cell reads exactly as today.
- A strictly increasing series yields `↑` or `↑↑`; flat yields `→`; decreasing
  yields `↓`.
- Engine `mention_velocity`, when present, takes precedence over the derived
  value.

**Tests.** `tests/test_mention_trend.py` — the four arrow cases, the
insufficient-samples case, engine-value precedence.

---

# Phase 2 — Volume truth

## T2.1 — Fix intraday RVOL (server-side)

**Goal.** This is a correctness fix, and it matters more than the display work.

**The problem.** `dashboard.py :: _vol_loop()` (line ~1100) computes
`rvol = day_vol / three_month_average_volume`. That is a full-day ratio compared
against a partial-day numerator. At 9:45 ET a stock trading at *five times* its
normal pace shows `rvol ≈ 0.2`. The number is not just imprecise, it is
systematically misleading in exactly the window Jonathan trades.

Meanwhile `tools/morning_funnel.py` already does this correctly:
`expected_fraction(mins_since_open)` interpolates a U-shaped cumulative volume
curve (heavy open, dead lunch, heavy close) so
`rvol = volume_so_far / (avg_vol × expected_fraction)`. That is the right method
and it is already in the repo, already tested (`tests/test_morning_funnel.py`).

**Files.** `dashboard.py` (`_vol_loop`), `tools/morning_funnel.py` (import only)

**Work.**
1. In `_vol_loop()`, import `expected_fraction` and `OPEN_MIN` from
   `tools.morning_funnel` (lazily, as the funnel loop already does — it pulls in
   pandas).
2. Compute minutes since the 9:30 ET open from `datetime.now(ET)` and divide the
   average by `expected_fraction(mins)`.
3. Write **both** keys so nothing downstream breaks:
   - `rvol_raw` — today's naive full-day ratio, preserved
   - `rvol` — the time-adjusted value
   Guard `expected_fraction` returning near-zero pre-market: floor the divisor.
4. Weekend/closed: fall back to `rvol_raw` rather than dividing by a curve that
   does not apply.

**Config.** `"rvol_time_adjusted": true` in `config/bot_config.json`. Set
`false` to restore the old math.

**Acceptance.**
- At 9:45 ET (15 min in, `expected_fraction ≈ 0.13`), a symbol at 13% of its
  average daily volume reports `rvol ≈ 1.0`.
- At 16:00 ET, `rvol ≈ rvol_raw`.
- Pre-market does not divide by zero or produce absurd values.
- `rvol_raw` still present on every row.

**Tests.** `tests/test_rvol_time_adjusted.py` — inject a fixed clock, assert the
9:45 / 12:00 / 16:00 cases and the pre-market floor. Do **not** hit yfinance in
tests; factor the math into a pure helper and test that.

---

## T2.2 — RVOL column in the monitor

**Goal.** Render it. A $2 stock on 8x volume is a different animal from one on
1.1x, and right now the momentum table shows no volume at all.

**Files.** `momentum_signal.py`

**Data.** Prefer `row["funnel"]["rvol"]` (funnel-scored, time-adjusted) →
fall back to `row["rvol"]` (T2.1) → `—`.

**Work.**
1. ```python
   def _rvol_cell(row: dict, hot: float, warm: float) -> str
   ```
   Renders `8.2x` / `1.4x` / `—`. Color: bold green ≥ `rvol_hot`, yellow ≥
   `rvol_warm`, dim below. Do not invent a value when both sources are missing.
2. Insert the column after `Chg%` via the T0.1 column spec, gated by
   `rvol_column_enabled`.
3. Add `rvol` to the `SymbolHistory` push so T3.1 can spark it later.

**Config.**
```json
"rvol_column_enabled": true,
"rvol_hot": 3.0,
"rvol_warm": 1.5
```

**Acceptance.**
- Funnel value wins over the top-level value when both exist.
- Missing data ⇒ `—`, never `0.0x`.
- Flag off ⇒ column absent, table identical to Phase 1 output.

**Tests.** `tests/test_rvol_cell.py` — source precedence, thresholds, missing
data.

---

# Phase 3 — Shape

## T3.1 — Price sparklines

**Goal.** `Chg%` is a scalar and hides shape. Building, spiking, and fading all
look the same. A 20-tick spark makes them distinguishable at a glance.

**Files.** New pure module `momentum-monitor/spark.py`; wired in
`momentum_signal.py`.

**Work.**
1. ```python
   def sparkline(values: list[float], width: int = 20) -> str
   ```
   Unicode blocks `▁▂▃▄▅▆▇█`. Rules that matter:
   - Fewer than 2 points ⇒ return `""` (not a flat bar — absence of data must
     not look like absence of movement).
   - All values equal ⇒ mid-level bar, no division by zero.
   - Downsample by taking the last `width` samples; do not average away spikes.
   - Scale per row (min→max of that row's own window), not globally.
2. Color the spark by net direction over the window: green up, red down.
3. Source data from `SymbolHistory.series(sym, "price")`.
4. Fixed-width column — pad to `spark_width` so the table cannot jitter as
   symbols accumulate history.

**Config.**
```json
"spark_enabled": true,
"spark_width": 20,
"spark_field": "price"
```

**Acceptance.**
- Monotonic rising series renders non-decreasing block heights.
- Constant series renders uniform mid blocks, no exception.
- Empty/one-sample series renders `""` padded to width.
- Column width is constant regardless of history depth.
- Flag off ⇒ column absent.

**Tests.** `tests/test_spark.py` — rising, falling, flat, empty, single-sample,
and a width-invariance assertion.

---

# Phase 4 — Memory

> This phase is the one that tells you whether the *other* phases were worth it.
> Everything above is a hypothesis about what helps. The journal is the only
> ticket that produces evidence.

## T4.1 — Session journal

**Goal.** Today, when a symbol leaves the feed it vanishes and nothing records
what happened next. There is no way to know the FOCUS hit rate, or whether
`rsi_focus_max: 35` is a good threshold or a lucky guess.

**Files.** New — `momentum-monitor/journal.py`; wired in `momentum_signal.py`.

**Work.**
1. Append-only JSONL, one file per session date:
   `momentum-monitor/journal/YYYY-MM-DD.jsonl`.
2. Write one record on each **rising edge** of an event — never per poll:
   ```json
   {"ts": 1753449600.0, "et": "09:47:12", "kind": "focus",
    "sym": "ABCD", "price": 3.41, "pct_change": 12.4,
    "rvol": 6.2, "mention_window": 9, "mention_count": 31,
    "cm_rsi": 22.0, "pctr": -91.0, "pctr_slow": -88.0,
    "proximity_pct": 100, "confluence": ["alert","squeeze"],
    "st_rank": 4, "session_window": "TRANCHE 3"}
   ```
   `kind` ∈ `{new, burst, focus, buy, st_new, st_look}` — the same rising edges
   `Alerter.fire()` already detects. Hook the writer next to the existing
   `alerter.fire(...)` call sites in `Feed.ingest()` plus a new FOCUS edge from
   T1.1's `focus_since` tracking.
3. Buffer writes and flush at most once per `journal_flush_sec` so the render
   loop never blocks on disk I/O. Flush on `KeyboardInterrupt` too — the desk is
   stopped with Ctrl+C and the last records must not be lost.
4. Add `momentum-monitor/journal/` to `.gitignore`. This is runtime state and
   may contain a full record of trading activity; it does not belong in git.
   Follow the existing pattern (`signal_state.json`, `trade_guard_state.json`
   are already ignored).

**Config.**
```json
"journal_enabled": true,
"journal_dir": "journal",
"journal_flush_sec": 5.0
```

**Acceptance.**
- One record per rising edge, no duplicates while the condition stays true.
- Ctrl+C flushes pending records.
- A disk error (permissions, full volume) logs once and does not stop the loop.
- Records are valid JSON, one per line, parseable by `json.loads` per line.

**Tests.** `tests/test_journal.py` — rising-edge dedupe, flush-on-close, a
write-failure path that does not raise, round-trip parse of a written file.

---

## T4.2 — Outcome backfill and hit-rate report

**Goal.** Turn the journal into an answer: *does FOCUS actually predict
anything?*

**Files.** New — `tools/journal_report.py` (CLI, matching the existing
`tools/paper_report.py` style).

**Work.**
1. Read one or more journal days. For each record, fetch bars via the existing
   `alpaca_api.fetch_bars_batch()` and compute forward returns at +5m, +15m,
   +30m from the record's `ts`.
2. Report, sliced by `kind`:
   - count, median and mean forward return at each horizon
   - hit rate (share with positive return) at each horizon
   - the same sliced by `rvol` bucket, `confluence.count`, and `session_window`
3. `--threshold-sweep` mode: recompute the FOCUS hit rate for a grid of
   `rsi_focus_max` and `pctr_focus_hi` values against the recorded `cm_rsi` /
   `pctr` fields, so the thresholds can be tuned on evidence rather than feel.
4. Print with `rich` tables, consistent with `paper_report.py`.

**Config.** None — CLI flags: `--days N`, `--date YYYY-MM-DD`, `--kind`,
`--threshold-sweep`.

**Acceptance.**
- Runs against a fixture journal without network access when bars are cached.
- Missing bars for a symbol are reported as excluded, not silently dropped from
  the denominator — a hit rate computed over an unknown sample size is worse
  than no hit rate.
- Sample sizes are printed next to every rate. Do not print a hit rate for
  n < 10 without flagging it.

**Tests.** `tests/test_journal_report.py` — forward-return math against a fixed
bar fixture, exclusion accounting, and the sweep producing one row per grid
point.

---

# Phase 5 — Signal quality

## T5.1 — Two-source confirmation as first-class state

**Goal.** Discord and Stocktwits independently surfacing the same symbol is the
strongest tell on the screen, and today it is a small `ST#4` chip lost among
other flags.

**Files.** `momentum_signal.py`

**Data.** The momentum table already computes `st_rank` (a dict of ST symbol →
rank) and passes it into `momentum_table()`. The dashboard's `confluence` field
covers `{scanner, chat, alert, squeeze}` but **not** Stocktwits — the ST poller
runs in the monitor process, not the dashboard. Compute the cross-source state
monitor-side; do not plumb Stocktwits into `dashboard.py` for this.

**Work.**
1. ```python
   def confirmation_level(row: dict, st_rank: int | None) -> int
   ```
   Counts distinct independent sources: dashboard `confluence.sources`, plus
   Stocktwits presence, plus `find_it_first`. Returns 0–N.
2. Render level ≥ 2 as a prominent row treatment — a `⚑CONFIRMED` badge and a
   background-colored symbol cell, visually louder than the existing flags.
3. Add a **header summary**: when any confirmed symbols exist, list them in the
   header panel (`header_panel()`) so they are visible without scanning rows —
   e.g. `CONFIRMED: ABCD ⚑2  WXYZ ⚑3`.
4. Emit a distinct `Alerter` kind `confirmed` on the rising edge (see T5.3).

**Config.**
```json
"confirm_enabled": true,
"confirm_min_sources": 2,
"confirm_header_summary": true
```

**Acceptance.**
- ST rank present + one dashboard confluence source ⇒ level 2 ⇒ badge shown.
- ST-only or Discord-only ⇒ level 1 ⇒ no badge, row renders as today.
- Header summary omitted entirely when nothing is confirmed (no empty line).
- Flag off ⇒ no badge, no header line, `ST#n` chip still renders as today.

**Tests.** `tests/test_confirmation.py` — level counting across source
combinations, badge on/off boundary, header summary empty case.

---

## T5.2 — Confluence-weighted ranking

**Goal.** `confluence.count` is computed and rendered as `⚡2` but never affects
position on screen. The strongest signal in the room can sit below a symbol
whose only distinction is that it arrived 30 seconds ago.

**Files.** `momentum_signal.py` — extends `row_rank()` from T0.1.

**Work.**
1. Add a confluence/confirmation term to `row_rank()`, weighted by
   `rank_confluence_weight`. Reuse `confirmation_level()` from T5.1 so there is
   one definition of "how many sources".
2. Keep the `new_ttl` freshness window as the dominant term by default —
   a brand-new symbol should still surface. Confluence breaks ties beneath it.
3. Gate with `rank_mode`: `"legacy"` (today's exact order), `"weighted"`
   (freshness + confluence + setup distance from T1.2). Default `"legacy"`.

**Config.**
```json
"rank_mode": "legacy",
"rank_confluence_weight": 1.0,
"rank_setup_weight": 0.5
```

**Acceptance.**
- `rank_mode: "legacy"` ⇒ ordering byte-identical to Phase 0.
- `rank_mode: "weighted"` ⇒ among symbols of equal freshness, higher
  confirmation level sorts higher.
- Ordering is stable — equal-scoring rows do not swap places between polls.
  (Jitter on a 2-second refresh is worse than a wrong order; assert this.)

**Tests.** `tests/test_row_rank.py` — legacy equivalence, weighted ordering,
and a stability assertion across repeated ranking of an unchanged row set.

---

## T5.3 — Alert escalation

**Goal.** Every alert currently beeps identically with a flat 60s per-(kind,
symbol) cooldown. A second burst on the same symbol is more meaningful than the
first; `FOCUS + BURST + ST#1` together is a different event than any one alone.

**Files.** `momentum_signal.py` (`Alerter`, `_beep`, `_macos_notify`)

**Work.**
1. Add a tier to `Alerter.fire()`: `tier` ∈ `{1, 2, 3}`.
   - tier 1 — single-source `new` / `st_new`
   - tier 2 — repeat burst on a symbol already seen, or `focus`
   - tier 3 — `confirmed` (T5.1), or `focus` on an already-confirmed symbol
2. Distinct sounds per tier. `_beep()` currently emits a single 880Hz tone on
   Windows; give tier 2 a double beep and tier 3 a triple or a rising pair. On
   macOS, `_macos_notify()` already takes a `sound` argument — map tiers to
   different system sounds.
3. Tier-scaled cooldown: `alert_cooldown × tier_cooldown_mult[tier]`. A tier-3
   event should be able to interrupt a tier-1 cooldown on the same symbol.
4. The existing global notification throttle (`alert_notify_interval`, default
   180s) must **not** suppress tier 3. Add a `tier_bypass_throttle` threshold.
5. Show the tier in the footer's recent-alert log (`Alerter.recent`).

**Config.**
```json
"alert_tiers_enabled": true,
"tier_cooldown_mult": {"1": 1.0, "2": 0.5, "3": 0.25},
"tier_bypass_throttle": 3
```

**Acceptance.**
- Tier 3 fires even when a tier-1 cooldown is active for the same symbol.
- Tier 3 bypasses the global notify throttle; tiers 1–2 do not.
- Flag off ⇒ single tone, flat cooldown, exactly as today.
- `alert_only_when_hidden` behavior is preserved for tiers 1–2.

**Tests.** `tests/test_alert_tiers.py` — cooldown scaling, tier-3 interrupt,
throttle bypass boundary, flag-off equivalence. Stub `_beep` and the notifier;
tests must not make noise or raise OS notifications.

---

# Phase 6 — Context

## T6.1 — Time-of-day awareness

**Goal.** `session_line()` sits in the header but nothing *behaves* differently
at 9:31 versus 12:15. `session_clock.py` already encodes the whole tranche
schedule — the monitor just does not act on it.

**Files.** `momentum_signal.py`, `session_clock.py` (read-only)

**Work.**
1. Per-window threshold overrides. A `session_overrides` config block keyed by
   the labels `session_window()` already returns — the nine in `WINDOWS`
   (`PRE-MARKET SCAN`, `T1 FUNNEL`, `TRANCHE 1`, `T2 FUNNEL`, `TRANCHE 2`,
   `T3 FUNNEL`, `TRANCHE 3`, `WIND-DOWN`, `EXITS ONLY`) plus the `CLOSED`
   fallback returned outside them and on weekends:
   ```json
   "session_overrides": {
     "EXITS ONLY": {"rsi_focus_max": 30.0, "alert_new": false},
     "WIND-DOWN":  {"alert_new": false}
   }
   ```
   Resolve the effective config once per loop:
   `effective_cfg(cfg, label) -> dict`. Only keys present in the override block
   change; everything else falls through.
2. Visually mark the dead zone. During `EXITS ONLY` / `WIND-DOWN`, dim the
   momentum panel border and show the guidance string from `session_window()`
   in the header — the existing `WINDOWS` table already carries copy like
   *"chop hours — be flat or managing, never entering"*.
3. Countdown to the next tranche from `next_shot()` in the header when one is
   pending.
4. Default `session_overrides` to `{}` — no behavior change until it is filled
   in deliberately.

**Config.**
```json
"session_aware": true,
"session_overrides": {},
"session_dim_windows": ["WIND-DOWN", "EXITS ONLY", "CLOSED"]
```

**Acceptance.**
- Empty `session_overrides` ⇒ effective config identical to base config in
  every window.
- An override applies only inside its window and reverts on exit.
- Weekend/`CLOSED` resolves without raising (`session_window()` returns
  `CLOSED` outside weekday windows).
- Countdown hides once the last shot has passed (`next_shot()` returns `None`).

**Tests.** `tests/test_session_overrides.py` — inject fixed ET datetimes at
09:35, 12:30, and a Saturday; assert override application, reversion, and the
`None` countdown path.

---

# Phase 7 — Fundamentals

> Scoped to **free sources only**, per decision. Read the honesty note in T7.1
> before building — the constraint here is mostly not about money.

## T7.1 — Float and short-interest provider

**Goal.** For sub-$30 momentum names, low float plus high mention velocity *is*
the setup. Float is the difference between a runner and a wall.

**Files.** New — `momentum-monitor/fundamentals.py`

**Honesty note — read this first.**
- **Float** is reported quarterly in SEC filings. It is stale by construction.
  For a small cap that just did an offering, a float figure can be badly wrong.
  Display the as-of date next to it; a float number without a date invites false
  confidence.
- **Short interest** is worse: FINRA collects it on settlement dates twice a
  month and publishes on a lag. *Every* source — free or paid — is republishing
  the same delayed FINRA data. Paying does not make it fresher. Anything sold as
  "real-time short interest" is a model estimate, not the reported figure.
- Practical consequence: treat both as **slow context**, never as triggers.
  Do not let them gate alerts, and do not let them into `row_rank()`.

**Free options, in order of preference:**

| Source | Gives | Cost | Risk |
|---|---|---|---|
| **yfinance** (already in `requirements.txt`, already used in `dashboard.py :: _vol_loop`) | `floatShares`, `sharesShort`, `shortPercentOfFloat`, `sharesShortPriorMonth` via `.get_info()` | free | unofficial, rate-limited, breaks on upstream changes; `.get_info()` is far slower than the `fast_info` path already in use |
| **FINRA short-interest files** | official reported short interest | free | bulk file, twice monthly, needs its own fetch + parse + cache |
| **Finnhub `/stock/profile2`** (key already configured) | `shareOutstanding` | free tier | outstanding ≠ float; can overstate badly for insider-heavy small caps |

**Recommendation:** yfinance as the single provider, behind an interface, with
aggressive caching. Do not add a second provider until the first proves
insufficient.

**Work.**
1. ```python
   class Fundamentals:
       def get(self, sym: str) -> dict | None
       # {"float_shares": int|None, "short_pct_float": float|None,
       #  "shares_outstanding": int|None, "as_of": str|None,
       #  "fetched": float, "source": "yfinance"}
   ```
2. **Cache hard.** Disk cache at `momentum-monitor/cache/fundamentals.json`,
   TTL `fundamentals_ttl_hours` (default 24). This data changes quarterly; there
   is no reason to fetch it twice in a session.
3. **Never fetch in the render loop.** Background thread with a bounded queue,
   fed by symbols appearing in the feed. The UI reads only the cache and shows
   `…` on a miss. This is the single most important constraint in the ticket —
   `.get_info()` can take seconds per symbol.
4. Rate-limit to `fundamentals_max_per_min` (default 10) and back off on errors.
   A failed lookup caches a negative result with a short TTL so a delisted or
   unrecognized symbol is not retried every loop.
5. Add `momentum-monitor/cache/` to `.gitignore`.

**Config.**
```json
"fundamentals_enabled": false,
"fundamentals_ttl_hours": 24.0,
"fundamentals_max_per_min": 10,
"fundamentals_cache": "cache/fundamentals.json"
```
Default **off** — this ticket adds a network dependency to a process that
currently has only two well-understood ones.

**Acceptance.**
- No network call originates from the render loop; verify by asserting the
  fetch runs on a non-main thread.
- Cache hit returns without network.
- Provider raising ⇒ `get()` returns `None`, loop unaffected.
- Rate limit is honored across threads.
- Stale-cache behavior: expired entries are served (with `fetched` intact) while
  a refresh is queued, rather than returning `None` and blanking the column.

**Tests.** `tests/test_fundamentals.py` — stub the provider entirely (no
network). Cache hit/miss/expiry, negative caching, rate limiting, exception
containment.

---

## T7.2 — Float and short-interest display

**Goal.** Surface low float and elevated short interest without adding noise.

**Files.** `momentum_signal.py`

**Work.**
1. Do **not** add two more columns — the momentum table is already at eight and
   gaining RVOL and a sparkline. Render as **flags** in the existing flag column:
   - `🪶LOW FLOAT` when `float_shares < float_low_threshold`
   - `SI 24%` when `short_pct_float >= short_pct_threshold`
2. Show the `as_of` date in the ST/detail panel or on hover-equivalent (the
   footer FOCUS line), not in the row. Per T7.1, an undated float figure is
   misleading.
3. Missing data renders nothing at all — no placeholder. An absent flag must not
   be readable as "float is fine".

**Config.**
```json
"float_flag_enabled": false,
"float_low_threshold": 20000000,
"short_pct_threshold": 20.0
```

**Acceptance.**
- Flags appear only above threshold and only with data present.
- Unknown float ⇒ no flag, no placeholder glyph.
- Flag column layout does not shift existing flags.
- `fundamentals_enabled: false` ⇒ no flags regardless of this ticket's flag.

**Tests.** `tests/test_float_flags.py` — threshold boundaries, missing-data
silence, and interaction with `fundamentals_enabled`.

---

# Suggested working order

Phases 0 → 3 are self-contained screen improvements and can ship in a week of
evenings. **Then stop and run Phase 4 for two weeks of live sessions before
building Phase 5.** T4.2's threshold sweep will tell you whether
`rsi_focus_max: 35` and the `%R` band are right, and the per-`kind` hit rates
will tell you which alerts deserve escalation in T5.3. Building Phase 5 first
means guessing at exactly the numbers Phase 4 measures.

Phase 7 is genuinely optional. Given that both float and short interest are
stale by construction, the honest expected value is "useful context on a
watchlist review", not "better intraday decisions".

---

# Definition of done, every ticket

- [ ] Feature flag added to `DEFAULTS` in `momentum_signal.py`; JSON file updated
      only if the shipped value differs from the default
- [ ] Flag off (or default) reproduces current behavior exactly
- [ ] New logic lives in pure functions, testable without a live feed
- [ ] Tests added under `tests/`, passing via `pytest`
- [ ] `ruff check` clean at `line-length = 120`
- [ ] No new network call inside `main()`'s render loop
- [ ] Exceptions contained — a failure degrades one cell, never the loop
- [ ] Manual check: run against the live dashboard for one session with the flag
      on, and one with it off
