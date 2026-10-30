# buystop_flow power amendment — proposal

2026-10-30. Running list row 53. This file is a proposal. It is not part of `docs/studies/buystop_flow_prereg.json`. Do not fetch, do not rebuild the sample, and do not score until two result-skeptic rounds have been applied and the operator says READY.

## What was already seen

The 2026-10-24 count, and nothing else. No arm NET was scored. From the mini's `count.json`:

- Half A: 2,235 G1-IN trades, 55 days, MDE 0.00030256 (3.026 bp), `floors_met` false.
- Half B: 2,319 G1-IN trades, 55 days, MDE 0.00024394 (2.439 bp), `floors_met` true.
- Frozen OFI60 cut from half A: `[0.2882797731568998, 0.057809488368586855]`.
- Frozen QI cut from half A: `[0.2, 0.7704854285098521]`.
- Sample: 7,975 trades a half, 145 a day, half A days 2026-05-11 .. 2026-07-29. `fetched.json` exists. Arm B was not fetched.

The miss is one day of dispersion. `55 * (3.026 / 3.0)^2 = 55.96`. A one-day add sized off that number is rejected here.

## The only change

Add every warehouse session in `[2026-04-27, 2026-05-11)` to half A. Confirmed on the mini 2026-10-30: the warehouse has 120 sessions, 2026-04-27 .. 2026-10-15, and that gap is these ten days:

2026-04-27, 2026-04-28, 2026-04-29, 2026-04-30, 2026-05-01, 2026-05-04, 2026-05-05, 2026-05-06, 2026-05-07, 2026-05-08.

They sit after the held-out window (2026-01-14 .. 2026-04-24) and before the frozen sample. Take all ten. None of them is optional, and none is chosen by its control dispersion.

If this block still leaves half A's MDE above 3 bp, the study stays UNDERPOWERED. No second block of sessions is added after seeing that number. A further add would be a new prereg.

## What stays frozen

- Pass rule, t bar 2.24, 98.75% bound, R = 0.05009, both arms, drop-3-days and drop-5-names, floors of 300 trades and 20 days, MDE bar 3 bp.
- Half cut `2026-07-29`. Half B's 55 days are not redrawn and not refetched.
- Held-out `warehouse_confirm` 2026-01-14 .. 2026-04-24 is not read.
- The 7,975 + 7,975 already sampled trades and `fetched.json` are not redrawn. Do not call `select_trades` with a new day count on the old rows.
- The OFI60 cut and the QI cut above. Do not refit them on the enlarged half. New trades are labeled with these cuts. Refitting would change who is G1-IN inside the trades already fetched.
- `wr_trend_trade.FIRST` stays `2026-05-11`. Other studies use that constant. The ten days are built only inside this study, with the same `t2_step` / stop re-derivation `build_sample` already uses.
- `config/bot_config.json` is not edited.

## How the ten days are built

For each new day, same seed `bsf-sample`, same stop re-derivation, at most 145 trades with entry price >= $20. A day with fewer than 145 candidates contributes only what it has. Do not backfill from the frozen days.

The T2 builder stores a control return and a bar gross, as the original sample does. The count step reports trades, days, and the control-return MDE only. It does not print mean gross or mean NET.

MDE is the registered formula: `(2.24 + 0.84) * SD(day means of G1-IN control returns) / sqrt(days)`.

If the new days' control dispersion matches the first 55, 65 days would print about `3.026 * sqrt(55/65) = 2.78` bp. That is a projection, not a result. The count is recomputed on the combined half A. Half B's count is kept. `--score` runs only if both halves then have `floors_met`. Otherwise it refuses and no outcome is read.

## Arm B, only after that count passes

`--cuts` loads `ai_reports/buystop_flow/pinned_bot_config_837dfd79.json` and refuses unless that file's sha256 prefix is `837dfd79268ebf16`. It does not read `config/bot_config.json`. The live file's prefix on 2026-10-30 is `4c823ea5c0645068`. Using it would score arm B on a different exit config than the one that was registered.

## Fetch

After READY only. Data-only account. One heavy Alpaca job at a time. Not during another fetch, and not added to `scripts/night_2026-10-30.sh` unless the operator says so. About 1,450 arm-A trade fetches (10 days × 145). Arm B's print fetch waits until the new count is powered.

## Confidence

About 70% that the enlarged half A prints an MDE at or under 3 bp, if the ten days are no noisier than the first 55. They can be noisier, and then the study stays UNDERPOWERED on purpose.

The chance that a later score PASSES stays the registered prior, about 15%. The realistic case in amended_2 is about 0–1 bp net after the quoted spread. This amendment does not raise that.
