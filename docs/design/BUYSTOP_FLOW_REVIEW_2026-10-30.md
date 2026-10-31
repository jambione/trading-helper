# buystop_flow uncommitted edits

2026-10-30. Running list row 53. Review of the edits that have sat on the MacBook since 2026-10-24. This commit records them. It does not add sessions and it does not score.

## What they are

The 10/24 handoff listed nine code-review fixes and said they were not applied yet. The working tree applies them, then records what happened the same day before any quoted-NET score:

- Arm B's last print chunk ends at `min(T + MAX_HOLD + 60, 15:50)`. A second split across a chunk keeps the lowest low and the later last price. A missing exit bid is counted, and a half under 300 trades or 20 days is UNDERPOWERED rather than a pass.
- The 98.75% bound calls `t_ppf(0.9875)`.
- Module hashes cover the exit stack the walk actually imports, including `ai_positions`, `bars`, `config`, `leash_replay`, `sell_double_check`, and `ai_entry_watch`.
- The probe times real `fetch_one` plus `fetch_arm_b` on trades spread across the sample. A sample built by other code is refused.
- An empty SIP condition list is a round-lot trigger. OFI60 still drops empty codes. The $10-$20 cell is declared dropped. G2, G3, quoted spread by side, and SPY-hedged NET are information only.
- The operator raised the fetch budget from 6 hours to 9 hours after the 6-hour probe put arm A's projected MDE at 3.19 bp. The 3 bp bar, the pass rule, and the sample did not change. The 9-hour probe projected 2.63 / 2.23 bp and the fetch ran.

The count, copied from the mini's `count.json` (outcome fields only: trades, days, MDE, floors), is now in the prereg:

- Half A: 2,235 G1-IN trades, 55 days, MDE 3.026 bp, floors not met.
- Half B: 2,319 trades, 55 days, MDE 2.439 bp, floors met.
- Status: UNDERPOWERED. `--score` was not run. Arm B's print fetch was not run.

The frozen sample is 7,975 trades a half, 145 a day, sessions 2026-05-11 .. 2026-07-29 for half A and the matching half B through 2026-10-15.

## What was checked

`tests/test_buystop_flow.py`: 16 tests, no network. They cover the round-lot rule, the chunk merge, the 98.75% quantile, the 9-hour budget, the spread-out probe, the arm-B floor, and the frozen prereg window. The live config file was hashed and not edited. Its prefix is `4c823ea5c0645068`, which is not the pin `837dfd79268ebf16`. `desk_cfg()` still reads `config/bot_config.json` and refuses a different prefix, so an arm-B run on today's file stops. The power-amendment proposal sends that run to the pinned file. This commit does not change that function.

## What this commit does not do

It does not change the pass bar, the halves, or the held-out window 2026-01-14 .. 2026-04-24. It does not add the ten warehouse sessions 2026-04-27 .. 2026-05-08. That amendment is a separate proposal and stays out of the prereg until the skeptic rounds and the operator say READY. `config/bot_config.json` is untouched. The callout-exit prereg and `name_history.py` edits on the working tree are a different study and are not in this commit.
