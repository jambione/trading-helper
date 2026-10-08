# Hand-back: history-study tools (name history, round numbers, video setups S2-S7)

Brief: `docs/briefs/history_studies_tools.md`. Branch `studies-history-tools` (not merged). No data was fetched and
nothing was run against Alpaca. No prereg or live-desk file was changed.

## What was built

| File | What it does |
|---|---|
| `tools/studies/bars_structure.py` | Pure functions: 5/15/60-min RTH aggregation aligned to 09:30 (60-min half bar), `completed()`, strict 2/2 swings with `known_ts`, `swings_at(t)`, 60-min concatenation across sessions, `UP60`, `HTF_UP`, the name_history event and T1 race, T2 pairs, T3, the predicates (rejection, momentum, pin, bullish FVG), LEVELS / S5 levels / round-number grids and groups, ATR14 / SMA50 / 20-day vol |
| `tools/studies/name_history.py` | `StudyMarket` (subclass of `bro_sr_wr.AlpacaMarket`: daily bars, per-symbol 1-min month files, **`quote_ts` returns the quote timestamp**, offline mode), universe, outcome-blind events and features, same-name control (seed 61, hour fallbacks), two-way clustered FE OLS, H1 / H2 / round-number verdicts, information cells, `fetch` / `count` / `score` / `report` |
| `tools/studies/structure_pullback.py` | Scanners S2-S7, exits (stop priority, gap fill, slippage cell, +80 min / 15:50 time exit, scan from t+60 s), same-geometry control (sha256 seed 67), paired raw and hedged series, the outcome-blind count step with the extension decision, verdicts, information cells |
| `tests/test_bars_structure.py`, `tests/test_name_history.py`, `tests/test_structure_pullback.py` (+ `tests/_hist_synth.py`) | 89 tests on synthetic bars, no network |

Both tools share one bar and quote cache (`ai_reports/history_studies/cache`). `count`, `score` and `report` never
touch the network: a cache miss counts as a fetch failure. `score` refuses to run until `count` has frozen the groups
(name_history) or the signals (structure_pullback) with a sha256, and refuses when the hash no longer matches.
structure_pullback `score` also refuses when the count step says to extend the sample and `--extend` was not given.

## Resolutions

The full text of each is in the `RESOLUTIONS` dict of each tool, and is copied into `result.json`. The ones a reviewer
should check first:

- **Adjusted bars from factors (name_history R2).** Adjusted 1-min prices are raw 1-min prices × that session's factor
  (adjusted daily close / raw daily close). Alpaca adjusts with one factor per date, so this equals adjusted bars and
  halves the fetch. A split is detected when the factor ratio between consecutive sessions moves by more than 10% (R3).
- **Early closes (R4 / SP R20).** Sessions that close before 16:00 are excluded and counted.
- **Terciles (R9 / R10).** TREND_SCORE uses per-session mid-rank percentiles. Its terciles are pooled over all test
  events.
- **T1 / T2 details (R6 / R7).** The T1 race uses bars from the event close to min(+30 min, 15:30). T2 uses
  close-to-close 15-min returns within a session, adjacent buckets only, pooled Pearson.
- **HTF_UP (R11).** The EMA20 runs on adjusted 60-min bars. It needs at least 140 completed bars, else it is missing
  and the event is left out of H2 (counted).
- **Control minutes (R15).** Controls are drawn from clock minutes, so no data is read to choose one. "Not within 30
  min" means |m − t| > 30 min. Fallback is the later hour, then the earlier hour.
- **Control set (R16).** One control per event, shared by H1 TOP, HTF_UP true and round-number ABOVE. The round-number
  projection uses only the H1 TOP ∪ HTF_UP controls.
- **Statistics (R17).** FE by alternating projections. V = V_session + V_name − V_session×name, each CR1. If V ≤ 0,
  use max(V_session, V_name). df = min(sessions, names) − 1.
- **Drop share (R18 / SP R18).** A unit's share is its contribution to the simple group-mean (or paired) difference.
  The drop test re-runs the same estimator.
- **Round-number projection (R22).** SE_ctrl × √n_ctrl × √(1/n_ABOVE + 1/n_BELOW), per half. This frozen projection
  decides "powered" for round numbers. "ABOVE beats its control" uses the POSITIVE rule, t ≥ 1.64 (R23).
- **FAILED-DATA (R24 / SP R16).** Drop rates are compared per hypothesis for name_history and round numbers, and
  pooled across arms for structure_pullback (per setup also reported).
- **Entry window (SP R2).** When a candidate's first qualifying entry bar falls outside 10:30-14:30, the candidate
  resolves with no entry (counted) and the scan continues.
- **Stale entry quote (SP R3).** It drops the pair and uses up that setup's one trade for the day.
- **S3 break (SP R5).** The break must be the bar immediately after the 4-bar consolidation.
- **Resume points (SP R7-R11).** S4 restarts the pole scan at the bar that ended the candidate. S5, S6 and S7 resume
  after the resolving bar or instant.
- **Time exit (SP R12).** The quote is taken at min(entry + 80 min, 15:50:00). Stop and target bars run from t + 60 s
  to the fallback bar ending at min(t + 80 min, 15:50).
- **Missing hedge (SP R13 / R23).** A stale SPY quote or missing beta takes the pair out of the hedged series only.
- **Not implemented (SP R19).** Two information cells are not computed; see below.

## Ambiguities and contradictions found in the preregs

1. **The S6 cancel can never fire on complete minute data.** A 5-min close below the FVG bottom (< top) means one of
   that bar's 1-min lows was ≤ the top, and that minute is checked before the 5-min bar closes. So the touch always
   comes first. The cancel only matters when minute bars are missing.
2. **The S3 "BREAK = the next completed bar"** can be read as the very next bar, or as the next bar that qualifies. I
   took the very next bar (R5); the other reading lets M from an old consolidation persist.
3. **The S7 "FIRST 60-min close above H today"** is checked against the H in force at each bar's start. Two
   indications with different H on the same day are possible after a resolve.
4. **Name_history RANGE BUDGET asks whether "the 3R target lies beyond the remaining ATR budget",** but name_history
   has no stop or target. It is computed only in structure_pullback (R30).
5. **Two structure information cells are not coded:** "setup 1 short mirror" (no rule given for "momentum fade" or
   "minor support") and "the desk's own %R square ... same 3R bracket" (no stop given for the square's bracket). These
   need a prereg addendum before they can be coded.
6. **Round-number power.** "Projected two-way SE from the same-name control arm, scaled to the ABOVE and BELOW group
   sizes" does not give a formula; R22 is my reading. The name_history control arm only covers TOP / HTF_UP events.
7. **Extension (structure).** "Extend the primary sample back to 2025-11-03" does not say whether 2025-11-03 is the
   first test session or the first look-back session. I applied the same rule as the primary sample, so the first 10
   sessions are look-back (R22).
8. **"Within 30 min" (name_history control) and "within 60 min" (structure control)** are read as inclusive, so those
   minutes are excluded.
9. **Bound inclusions.** "Close in the upper half" is read as ≥ the midpoint, the R bounds include 0.15% and 2.0%, and
   "target on >=" follows the prereg.
10. **Reruns.** The out-of-period and IEX reruns a PASS requires are not wired yet. They need a `--period` argument and
    a feed switch (IEX bars, SIP quotes). That is a small follow-up, not needed before the first scoring.

## Tests

`pytest -q` over the whole suite: 4232 passed, 6 failed, 7 skipped. The 6 failures (`tests/test_alert_sound.py` ×5 and
`tests/test_sr_breakout_book.py::test_real_pins_accept_head_and_reject_dirty`) fail the same way on the untouched base
`master-mac` in this container: alert-sound playback, and a git-HEAD build check. `config/bot_config.json` was
backed up before the run and is unchanged.
