# After the close — 2026-10-01

The intraday desk's working list for after today's close. Jonathan observes the
session and adds items; Claude keeps this file current and adds whatever the
day's checks turn up. Newest items at the bottom of each section.

## Jonathan's observations

_(none yet)_

## From today's checks (Claude)

- [ ] Engine FLY fix (681c933) first live day: compare logged %R with IEX-rebuilt %R at every fill (`pctr_check.py` in the scratchpad / `/tmp/pctr_check.py` on the mini).
- [ ] Overnight: first scored night (~09:50), first -1% filtered buy (15:58), every kept name filled.

## Shipped before the open (watch today)

- [x] Engine %R counts minutes, not IEX rows (`rte_minute_grid`, default on). FLY 9/30 12:16 now reads -38 / -75 vs TradingView -33 / -71 (was -39 / -40). Shipped pre-open with Jonathan's OK. Watch: squares should now match the chart; arms may shift on thin names.
- [x] Overnight paper P&L matched first-in-first-out (leftover shares keep their own buy price).

## Open engineering items

- [x] Item 3: overnight paper P&L first-in-first-out (d8b1c8b, live).
- [x] Item 4: engine %R in minutes (d2630f1, live pre-open).
- [ ] Item 5: the 7 tests that were already failing. Running in a separate session (task_6be76155).
- [~] Item 6: replay can test engine changes. Phase 1 shipped and verified (26ebb1d, `--engine recompute`): on the 9/30 FLY window, pre-item-4 vs item-4 code now differ (6 vs 7 opens, different FLY block reasons) and neither buys FLY. Phase 2 (replay the engine's realtime bar store from recorded prints, so seed/drop/re-add bugs like FLY's reproduce) not started.

## Context

- 9/30 replay: 55 opens, -1.4 bp/trade gross; costs ~10 bp a trade on top (spread).
- Every entry/exit/filter variant tested since August scores ~0 gross (see memory: exits-cannot-move-the-mean, entry-setups-are-noise-equivalent, the-universe-fades-at-every-horizon). Ideas worth trying are ones that change *which names* or *what information* the desk uses, not another threshold.
