# Breakout distance on 91 untouched sessions: both pre-registered tests FAIL

> **RETRACTED (skeptic, same evening): the "squares lose to random minutes" information section is an ARTIFACT.**
> - **The control used hindsight.** Control minutes were drawn from the same clock hour as the square, and that
>   hour is in the sample *because* price rose in it. Controls before the square had that rise in their forward
>   window: +66.8 / +44.8 bp gross.
> - **With honest controls, squares are a coin flip.**
>   - Square minus control windows that start after the square: −3.8 (t −1.4) / +1.5 (t +0.6).
>   - Square minus every universe minute in the same day and hour: −6.0 (t −2.8) / +0.8 (t +0.6).
>   - Squares themselves are about 0 bp gross (−1.0 / +3.2) and about −20 bp net. The loss is the cost.
> - The 10/02 study's "squares −21 / −24 vs random" used the same control, so it is the same artifact.
> - The pre-registered H2/H3 FAILs (squares vs squares) stand.

Pre-registration: [`sr_breakout_history_prereg.json`](sr_breakout_history_prereg.json) (52eebd3, amended 771b38e before any run).
Script: `tools/studies/sr_breakout_history.py`. 91 sessions 2026-05-01..09-10, top-40 $10+ gappers (raw open),
3,474 name-days, 17,032 square moments, no failed days, no low-coverage days.

## Verdict (pre-registered)
- **H2 chase (>= 0.30% above a just-broken sell zone) worse than squares with no breakout: FAIL** (H1 -3.1 bp t -0.48, H2 +10.5 t +1.04).
- **H3 clean (0.10-0.30%) better: FAIL** (H1 -1.8 t -0.37, H2 +5.9 t +2.05; pooled +2.4 t 0.85).
- Breakout distance does not rank square moments. The SR column's green/red "brk" is a visual aid only; the 10/5 leads did not replicate.

## Information (not the pre-registered question, but the largest effect in the run)
Every square moment did worse than a random minute in the same name, same day, same clock hour (event minus control, net15):

| band | half 1 | half 2 |
|---|---|---|
| none (n 6,398 / 6,794) | -24.0 | -13.3 |
| poke | -7.3 | -9.7 |
| clean | -26.4 | -11.7 |
| chase (n 1,278 / 857) | **-49.1** | **-27.8** |

This replicates the 2026-10-02 levels study (square arms -21 / -24 bp vs same-name same-hour random minutes, 9/04-10/02) on
91 independent earlier sessions. The square trigger (both %R lines >= -20) selects worse-than-random moments in these
names, and chase squares are the worst. **Needs a skeptic review and its own pre-registered test before any decision**
(e.g. square vs a non-square trigger on new sessions); it is not a config change.

## Raw report
```
# Breakout distance on untouched history (prereg docs/studies/sr_breakout_history_prereg.json)

sessions 91 (2026-05-01..2026-09-10), name-days 3474, events 17032, failed days []
halves: H1 2026-05-01..2026-07-07, H2 2026-07-08..2026-09-10
drops {'raw_open_below_10_or_no_rth': 66, 'exit_tolerance_poke': 2, 'exit_tolerance_chase': 11, 'exit_tolerance_none': 44, 'exit_tolerance_clean': 1, 'split_in_window_prior_dropped': 3, 'short_series': 1}
days with < 90% of requested names returned: []

- poke H1: n 297 mean -15.6 vs none -20.2 (n 6398) | DAY-FE diff +6.5 bp, t +0.97
- poke H2: n 394 mean -17.6 vs none -18.1 (n 6794) | DAY-FE diff +1.2 bp, t +0.59
- poke Hall: n 691 mean -16.7 vs none -19.1 (n 13192) | DAY-FE diff +3.5 bp, t +1.12

- clean H1: n 463 mean -24.2 vs none -20.2 (n 6398) | DAY-FE diff -1.8 bp, t -0.37
- clean H2: n 551 mean -14.2 vs none -18.1 (n 6794) | DAY-FE diff +5.9 bp, t +2.05
- clean Hall: n 1014 mean -18.8 vs none -19.1 (n 13192) | DAY-FE diff +2.4 bp, t +0.85

- chase H1: n 1278 mean -24.8 vs none -20.2 (n 6398) | DAY-FE diff -3.1 bp, t -0.48
- chase H2: n 857 mean -8.4 vs none -18.1 (n 6794) | DAY-FE diff +10.5 bp, t +1.04
- chase Hall: n 2135 mean -18.2 vs none -19.1 (n 13192) | DAY-FE diff +2.6 bp, t +0.46

INFORMATION: per band and half, mean net15 and mean (event minus same-hour random-minute control)
- none H1: n 6398 net15 -20.2 | minus control -24.0 (n 6382)
- none H2: n 6794 net15 -18.1 | minus control -13.3 (n 6773)
- poke H1: n 297 net15 -15.6 | minus control -7.3 (n 297)
- poke H2: n 394 net15 -17.6 | minus control -9.7 (n 394)
- clean H1: n 463 net15 -24.2 | minus control -26.4 (n 463)
- clean H2: n 551 net15 -14.2 | minus control -11.7 (n 550)
- chase H1: n 1278 net15 -24.8 | minus control -49.1 (n 1274)
- chase H2: n 857 net15 -8.4 | minus control -27.8 (n 855)

**H2 chase (>= 0.30%) worse than no breakout: FAIL**
**H3 clean (0.10-0.30%) better than no breakout: FAIL**
(each: in BOTH halves the difference is >= 5 bp in the predicted direction with |t| >= 2, pooled |t| >= 2.24, >= 100 events per half)
```

## Addendum 2026-10-10: raw-price floor check (nothing to re-score; verdicts unchanged)

The split-adjusted universe problem ([ROOM_HOD_OOS_2026-10-09.md](ROOM_HOD_OOS_2026-10-09.md)) was already handled
here. The amended prereg required a RAW 09:30 open >= $10 from raw minute bars, and the script dropped 66 name-days
(`raw_open_below_10_or_no_rth`). An independent check (`tools/studies/rawfloor_rescore.py`,
[RAWFLOOR_RESCORE_2026-10-10_raw.md](RAWFLOOR_RESCORE_2026-10-10_raw.md)) found the same 66 below-$10 name-days in the
5/1-9/11 universe. **0 of the 17,032 scored events (0 of 3,127 scored name-days) came from a name below $10 raw.** All
outcomes were computed on raw bars. The H2/H3 FAILs and the retraction above stand as written.

Limitation, shared with SIP_BREAKOUT_2026-10-03: the top-40 gap ranking was formed on adjusted prices before the raw
filter, so the dropped slots were not refilled.
