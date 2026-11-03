# buystop_flow power amendment — proposal

2026-10-30. Running list row 53. This file is a proposal. It is not part of `docs/studies/buystop_flow_prereg.json`. Do not fetch, do not rebuild the sample, and do not score until two result-skeptic rounds have been applied and the operator says READY.

Round 1 (result-skeptic, 2026-10-30): FIX FIRST, 7 items. Applied verbatim below. The 2.78 bp projection is not a fetch gate.

Round 2 (result-skeptic, 2026-10-30): FIX FIRST, 3 items. Applied verbatim. No third review. This proposal waits for the operator's READY. It is not registered until then.

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

They sit after the held-out window (2026-01-14 .. 2026-04-24) and before the frozen sample; all ten stay in the block, none is dropped after its control dispersion or bar gross is known, and `warehouse_confirm` is not opened for any of them.

If this block still leaves half A's MDE above 3 bp, the study stays UNDERPOWERED. No second block of sessions is added after seeing that number. A further add would be a new prereg.

## What stays frozen

- Pass rule, t bar 2.24, 98.75% bound, R = 0.05009, both arms, drop-3-days and drop-5-names, floors of 300 trades and 20 days, MDE bar 3 bp.
- Half cut `2026-07-29`. Half B's 55 days are not redrawn and not refetched.
- Held-out `warehouse_confirm` 2026-01-14 .. 2026-04-24 is not read.
- The frozen 7,975 + 7,975 keys in `sample.json` and `fetched.json` stay as they are, are not passed through `select_trades` or `chosen_trades`, and are not rebuilt when `module_shas` changes; the ten-day rows live in a separate file, the enlarged count concatenates them after the frozen half-A keys, and `probe.json` `n_half` stays 7,975.
- Label old and new trades only with `groups()` at the frozen pairs `(OFI60, tie_u) = (0.2882797731568998, 0.057809488368586855)` and `(QI, tie_u) = (0.2, 0.7704854285098521)`, with `tie_u` from `order_flow_pilot.tie_u` on `(sym, day, fill minute k)`; do not call `top_cut` on any set that includes the new days, and refuse if `top_cut` on the original half-A rows alone is not those pairs.
- `wr_trend_trade.FIRST` stays `2026-05-11`. Other studies use that constant. The ten days are built only inside this study.
- `config/bot_config.json` is not edited.

## How the ten days are built

Build those days only by `batch2.t2_step` through the same T2 branch `build_sample` re-derives, using that day's main-warehouse names, IEX `lines` from the previous session and from today, today's SIP for the high-trigger and `px = max(stop, SIP open of k) >= $20`, split-jump skip, and a drop when `batch2.gross` is missing, with `ctrl_d` from `wr_trend_trade.d_control` tag `b2-T2-d`, sorted by `(sym, entry_min)`, shuffled with `seeded("bsf-sample", day)`, prefix at most 145, and not backfilled; 2026-04-27 has no previous warehouse session so it contributes zero trades and is not warmed on 2026-10-15 or on 2026-04-24; do not call `batch2.main`, do not write `ai_reports/batch2/rows.json`, do not change `wr_trend_trade.FIRST` or `t2_step`, and do not print any mean, sum, or list of bar gross or control returns.

Half A's MDE is recomputed only with the 10/24 function: G1-IN requires the same `fetch_one` quoted `net_A` and the frozen cut, the control is that trade's `ctrl_d`, and MDE is `(2.24 + 0.84) * statistics.stdev(day means) / sqrt(days that have a mean)`, never `sqrt(65)` and never 2.78 bp; the count report may print that single MDE number, trade counts, day counts, and `floors_met`, and must not print the day means or any mean, sum, or list of `net_A`, `net_B`, or bar gross; half B is recomputed on the frozen rows and frozen cut and the run refuses unless it is 2,319 trades, 55 days, 2.439 bp, `floors_met` true; `--score` runs only if both halves then have `floors_met`, and both arms use that same G1-IN set.

## Arm B, only after that count passes

`--probe`, `--cuts`, `--score`, and `arm_b_walk` start from `config.DEFAULT_CONFIG`, `update` it with `ai_reports/buystop_flow/pinned_bot_config_837dfd79.json` so the pin wins, drop `_min_hold`, refuse unless that file's sha256 prefix is `837dfd79268ebf16`, and pass that cfg into the walk; they do not call `desk_cfg()`, `load_config()`, or `load_config_ro()` and do not open `config/bot_config.json`, the live prefix `4c823ea5c0645068` is not a new pin, `--probe` is not re-run, and arm-B prints are fetched only after the enlarged count has `floors_met` and only for that G1-IN set.

## Fetch

After READY only. Data-only account. One heavy Alpaca job at a time. Not during another fetch, and not added to `scripts/night_2026-10-30.sh` unless the operator says so. Arm-A fetches cover the new days only, at most 145 trades a day. Arm B's print fetch waits until the new count is powered.

## Confidence

The 2.78 bp sketch and the 70% line are withdrawn because they assume the new days match the old control SD, the 10/24 projection already missed (2.63 bp versus 3.026 bp realized; 2.78 times that miss is about 3.20 bp), and neither number is a fetch gate or a reason to add or drop sessions; the chance a later score PASSES stays the registered prior of about 15%, and the realistic case stays about 0–1 bp net.
