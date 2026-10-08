# Brief: build the history-study tools (name history, round numbers, video setups)

Branch: create `studies-history-tools` from `master-mac` and push only to it. Do not merge. Do not touch the live desk.

## Goal

Implement three pre-registered studies **exactly** as specified. The preregs are the spec, and every rule in them is binding. Where a prereg is silent, choose the simplest reading, write it into a `RESOLUTIONS` dict in the tool, and list it in your final summary.

| Tool | Prereg |
|---|---|
| `tools/studies/name_history.py` (also computes the round-number groups) | `docs/studies/name_history_prereg.json`, `docs/studies/round_numbers_prereg.json` |
| `tools/studies/structure_pullback.py` (setups S2-S7) | `docs/studies/structure_pullback_prereg.json` |
| `tools/studies/bars_structure.py` (shared, pure functions) | used by both |

Read every prereg's `amended*` fields: they supersede the earlier text.

## Shared module `bars_structure.py` (pure, no network)

- 1-min bars → 5-, 15- and 60-min RTH bars aligned to 09:30 (60-min: 09:30-10:30 … 15:30-16:00 half bar). Each bar carries `start` and `end`; a bar is **completed** when `end <= t`.
- **Swing highs and lows** (strict 2/2 rule). Each carries `known_ts` = the end of bar i+2. Provide a 60-min version that concatenates RTH bars across sessions.
- Predicates: `rejection_bar`, `momentum_bar`, the pin-bar variant (S5), and bullish FVG detection (S6).
- Levels (name_history LEVELS and S5 levels; round-number grids). ATR14 and SMA50 from daily bars (information cells).
- `UP60`, `HTF_UP`, T1 / T2 / T3 exactly as the preregs define them.
- Every function takes the decision time `t` and must **never read a bar or swing that is not known by t**.

## Data access

- Reuse `tools/studies/bro_sr_wr.py`'s `AlpacaMarket`: paced, cached, `FetchFail`, `market_hours_guard`. Add a quote helper that **returns the quote timestamp** (the existing `quote()` does not), as the preregs require (≤ 5 s staleness rules).
- RAW prices for the universe, events, levels and costs; ADJUSTED prices only where the preregs say (look-back features, beta).
- Universe: top 400 by raw D−1 close × volume over the bro_sr_wr asset set, raw close ≥ $10.

## Commands (each tool)

`fetch` (after hours only), `count` (the outcome-blind power step where the prereg defines one), `score`, `report`.

- `count` must not read any event outcome. For structure_pullback it uses control outcomes only, with SE = √2 × the two-way clustered SE of the control.
- Two-way clustering (session × name) and `t_crit` from `bro_sr_wr.t_crit` with the prereg's z (2.24 for Bonferroni 2, 2.64 for Bonferroni 6, 1.96 for round numbers).

## Tests (required; no network)

Write `tests/test_bars_structure.py`, `tests/test_name_history.py` and `tests/test_structure_pullback.py` using synthetic bars. Cover at least:

- **Look-ahead guards:**
  - a swing is not visible before `known_ts`;
  - a forming bar is never used;
  - the S6 FVG is only known at the end of bar k+1;
  - the S7 indication uses a swing known at that bar's start;
  - control draws never read past their own time.
- Each setup S2-S7 firing on a constructed pattern and **not** firing on near-misses: first touch, a wick-only break, a cancel, the R bounds.
- Exit rules:
  - the stop wins when one bar hits both stop and target;
  - gap-through fills at min(stop, open);
  - the slippage-cell cap;
  - the time exit at +80 min;
  - exit scanning from t+60 s.
- Control rules: same geometry, the 60-min exclusion, the deterministic sha256 seed, hour fallbacks (name_history).
- The verdict logic for each prereg: PASS, FAIL (including powered-and-not-passed), UNDERPOWERED, and the drop-top-names/sessions tests.
- The round-number groups are exclusive and exhaustive.

## Must stay green

- `pytest -q` for the whole suite. The suite can rewrite `config/bot_config.json`: copy it before the run, restore it after, and commit no change to it.
- Touch nothing outside `tools/studies/`, `tests/` and `docs/briefs/`.

## Do not

- fetch data or run against Alpaca (the cloud cannot reach keys; data runs happen on the mini later);
- change any prereg;
- edit live desk code.

## Hand-back

Push the branch. Reply with:
- the commit hash;
- the test count;
- the `RESOLUTIONS` you chose;
- anything in the preregs you found ambiguous or contradictory.

A skeptic code review runs on the branch before any data is scored.
