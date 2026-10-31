# orb_in_play amended_6 — PROPOSED (score 2018-01-11 onward, halves re-split by date)

Status: **PROPOSED 2026-10-31 (AGY). Awaiting result-skeptic round 1.** Not READY. Nothing in
`tools/studies/orb_in_play.py` changes until the operator says READY after two skeptic rounds; then the code change
gets its own code review before the count runs.

## Why (coverage, not a result)

The amended_5 count aborted on coverage step (e) on 2026-10-31 (running list row 59). No count.json, no power number,
no outcome was produced. `fullday.pkl` (the only file with trade outcomes) was not opened by any check below.

Volume-only check, run 2026-10-31 on the mini with the amended_5 code's own `universe()` / `in_play()` on `daily.pkl`,
`daily_raw_close.pkl` and `open.pkl` (script: `tools/studies/orb_in_play_e_split_check.py`, run with `EEH_ROOT=<repo>`; it imports `orb_in_play` and never
loads `fullday.pkl`):

- 2,582 sessions fall in the amended_5 halves. **439 fail (e)**, first 2016-02-02, last **2018-01-10**. None after.
- Every failing day has a full 1,000 names (0 short universes). The 1,000th name's prior-20-day median dollar volume on
  failing days: min $9.93M, median $15.78M, max $19.99M.
- Why (e) exists: the ever-liquid list only holds names that reached $20M ADV at some point since 2016. On a day whose
  1,000th name trades under $20M, a true top-1,000 name that never reached $20M is missing from the list, so the universe
  can be wrong. That concern is real on those days, so they are dropped rather than waved through.

Earlier volume-only reads (Claude, 10/31) used the same inputs. Neither read any trade, control, gross, net or SPY
return.

## The change

1. **Scored window starts 2018-01-11**, the first session after the last (e) failure. End unchanged (2026-10-15).
   Held-out 2026-01-14 .. 04-24 and warm-up 04-27 .. 05-22 stay unscored and the held-out window stays unread.
2. **Halves re-split by date** at the median of the scorable in-play sessions in the new window (2,091 sessions, counted
   from volume only):
   - **H1 = 2018-01-11 .. 2022-04-05** (1,045 sessions)
   - **H2 = 2022-04-06 .. 2026-10-15** (1,046 sessions)

   Keeping the amended_5 split would leave 748 / 1,343; the even split gives the weaker half its best power.
3. **Look-back segments do not move.** The look-back breaks stay at 2021-01-04 and 2026-04-27 exactly as fetched
   (`segment()` keeps its own constant, no longer read from `HALVES`). So `universe()`, `unions()`, `lookback_misses()`
   and `in_play()` return what they returned for the fetch, and no refetch is needed. Declared: H1 now contains the
   2021-01-04 segment break; its ~20 warm-up sessions (no universe yet) are unscored, as they were under amended_5.
4. **Coverage step (e) stays a HARD abort**, applied to the new window. The check above finds zero failures there. If
   the code finds any, the study ABORTS. No further trimming of days, and no second start date.
5. **Coverage (a)-(d)** run by year on the new window. Rule (d) (2016 names-died vs 2017-19) no longer applies because
   2016 is not scored. Declared N/A, not silently dropped. If (a) or (c) excludes a year, exclusions stay final and the
   pooled MDE and per-half floors are recomputed before any outcome, as amended_5 says.

## Power

The gate is unchanged: pooled MDE <= 10 bp, and each half >= 300 long trades and >= 20 days. Projection, scaling the
amended_5 projection (7.6 bp at ~2,600 sessions) by sqrt(2,600 / 2,091): **~8.5 bp pooled**, inside the bar. This is a
sketch, not a gate. The count step computes the real MDE. If the pooled MDE comes out above 10 bp, the study stays
UNDERPOWERED and no sessions are added afterwards; adding sessions would need a new prereg.

## What stays frozen

- Signal, trade, control, costs (10 bp primary, 5 / 20 bp info), the beta-1 SPY hedge, and every amended_5 pass and
  FAIL rule: pooled day- and month-clustered t >= 2.0, each half net > 0 and hedged lift >= 0, pooled hedged, drop the
  best calendar year, batch-robust t >= 2.73, and the SELECTION ONLY / DECAYED / COST-FRAGILE / SIP-only labels.
- Universe rules, top 1,000 / top 20, $10 raw floor, ATR > $0.50, and OPEN_LB 14 / OPEN_NEED 10.
- Fetched files (`daily.pkl`, `daily_raw_close.pkl`, `open.pkl`, `open_union.pkl`, `fullday.pkl`). No fetch, no
  refetch.
- `config/bot_config.json` is not read or edited.

## Labels that change with the window (information only)

- "paper window 2016-2023" becomes **2018-2023**. "after publication 2024-2026" is unchanged.
- The COST-FRAGILE tag reads H1's 20 bp net, which is now 2018-01-11 .. 2022-04-05.
- The by-year table starts at 2018 (a partial year).

## Code change after READY (for code review, before the count)

- `HALVES = {"H1": ("2018-01-11", "2022-04-05"), "H2": ("2022-04-06", "2026-10-15")}`.
- New `SEG1 = "2021-01-04"`. `segment()` uses `SEG1` and `SEG2`, not `HALVES["H2"][0]`.
- `half_of()` returns None before 2018-01-11 (it follows from `HALVES`).
- The coverage (d) 2016 rule is skipped when 2016 is outside `HALVES` and reported as N/A.
- Info cell renamed `paper_window_2018_2023`. The declared strings are updated to the new halves and segment text.
- Tests:
  1. `segment()` output is identical to amended_5 on every fetched session.
  2. `lookback_misses()` is still 0.
  3. No day before 2018-01-11 reaches `build_rows`.
  4. (e) still raises on a synthetic low-cutoff day inside the window.
- The count runs with `--prereg-commit <amended_6 READY sha>`. `--score` only after `powered` is true.

## Confidence

Unchanged from amended_5: about 20-25% PASS (Claude, before any data). Dropping 2016-17 removes about two years of the
paper's own window, so a PASS is no more likely than before.
