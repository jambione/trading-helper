# %R accuracy on the live desk (2026-10-09)

**Status: plan. Nothing ships before the G2 read (10/15), and nothing ships mid-session.** No profit claim: this makes the desk act on
the same %R the operator sees; it does not create an edge by itself.

## Finding (10/9, `tools/studies/wr_parity.py`)

57 random live readings after 11:30, the desk's logged %R vs %R computed from full-tape SIP 1-minute bars at the same minute
(completed minutes only; fast = %R(21) EMA 7, slow = %R(112) EMA 3 on a minute grid, as `signals.py`):

| Line | Median gap | 80th percentile | Worst | Within 5 points |
|---|---|---|---|---|
| Slow (112) | 2.0 | 4.0 | 11.7 | 47 / 57 |
| Fast (21) | 4.6 | 9.1 | 38.8 | 31 / 57 |

Squares fire at -20, so a 9-point fast-line error can create or hide a square.
The studies are not affected: they compute %R from complete SIP bars, which is the accurate version.

## What already exists

- **Stale prices cannot arm.** All 1,481 `stale_tape` arm rows on 10/9 were refused (`stale_quote`; `ai_watch_arm_require_stream_price=true`).
  The worst reading (XYZ 13:02, live -13.6 vs tape -52.4) was on a stale source and could not trigger a buy.
- **Completed-minute %R is a setting.** `ai_watch_exhaustion_live=true` (live) recomputes %R mid-minute with the live trade as the close
  and folds it into the window high. `false` uses the engine's minute-grid %R from completed bars. The nightly replay already runs this as
  `pr_engine` / `presq_engine` (tight_trail_replay_prereg.json); those reads are frozen with the exit variants (d7f8b1c).

## Remaining causes

1. **IEX bars miss the real highs and lows.** The fast line (21-minute window) feels this most. Not fixable without real-time SIP, which
   is out until the desk is profitable (no-spend rule).
2. **Mid-minute recompute.** Part of the measured gap is the live value including the forming minute; part is the recompute itself.
3. **No parity check against the operator's TradingView indicator** (settings and readings still needed).

## Plan (after 10/15, after a close, one change at a time)

1. **Measure for 5 sessions, change nothing.** Run `wr_parity.py` nightly. Add one log field next to the live value: the engine's
   completed-minute %R (`r_engine`, `rs_engine`), so both can be compared to the tape on the same moments. Logging only.
2. **TradingView parity.** With the operator's indicator settings and ~5 readings (symbol, date, time, fast, slow from a 1-minute chart),
   compare `signals.py` on SIP bars. Within 1-2 points = the port is right; otherwise fix the port first.
3. **Candidate change: `ai_watch_exhaustion_live=false`.** Ship only if, over those 5 sessions, the engine value is closer to the tape
   than the live value (median fast gap at least 2 points lower and 80th percentile at most 6), AND the `pr_engine` replay over the same
   sessions nets no worse than base after costing. Otherwise keep live.
4. **Lean on the slow line where accuracy matters.** It already tracks the tape within ~2 points; any rule that needs a precise %R
   level should use it.

## What this does not touch

- Day-hold (scored from bars, not desk %R), G2, tonight's tests, and the frozen reads.
