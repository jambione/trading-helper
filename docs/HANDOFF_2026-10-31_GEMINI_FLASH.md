# Handoff 2026-10-31 → Gemini Flash (from AGY)

Read this first, then `docs/running_list.md` rows 27 and 54-63. The newest row is the truth. If a row and this note
disagree, ask the operator.

## Rules (non-negotiable, learned this week)

1. **Git:** work on `master-mac` only. `git pull --ff-only origin master-mac` before any commit. **Never** `push --force`,
   `reset --hard` on a pushed branch, or rebase pushed commits. (On 10/30 a force-push dropped 14 commits; repaired 10/31
   by a normal merge, `a4ac0723`.) If a pull won't fast-forward, stop and ask.
2. **Live config:** `config/bot_config.json` is live money. Do not edit it without the operator's explicit OK for that
   exact change. **Do not run `pytest` in the mini's checkout** (`ssh mac-mini-away`, `~/repo/trading-helper`): some
   test rewrites `config/bot_config.json` (seen 10/31 in a scratch worktree). Run tests on the MacBook only, and check
   `git status` for `config/` afterwards.
3. **Studies:** a prereg or amendment needs two result-skeptic rounds (reviewer text applied **verbatim**), then the
   operator's literal READY, before any code change or outcome is read. No outcome file is opened before READY. A
   powered FAIL is closed. No re-runs with tweaks (buystop row 27 stays closed).
4. **Mini:** one heavy Alpaca job at a time, on the data-only key, after hours. Deploy is `git pull --ff-only` on the
   mini. Restart the bot only if trading-path code changed, and tell the operator.
5. **Known baseline test failures (not yours to fix unless asked):** 7 fail on the MacBook before and after the 10/31
   merge: `test_effective_max_positions_scales_with_equity`, `test_no_new_state_is_written_on_the_arm_path`,
   4 × `test_sim_rstop_path`, `test_arm_ceiling_releases_a_20s_print_but_not_a_40s_one`. Any *new* failure is yours.

## State at handoff (master-mac, MacBook = GitHub = mini)

- Nothing is running on the mini.
- Seat-churn on at 600 s (row 56). Judge it on next week's trades.
- Closed: buystop_flow FAIL (row 27), marketable limit NOT USEFUL (row 60), never-green (57), %R family (54), slope-now (55).
- `eng/safe-alone-2026-10-30`: Grok's engineering commits (fill-matched trades, alarms, CI, config schema), **waiting
  for operator review**. Do not merge it.
- The 10/28 MacBook WIP is sorted: the name_history SIP-end fix + test, the callout_exits draft (NOT READY) and
  `tools/blocked_today.py` are on master-mac. The sizing simulators sit on the local branch `parked/sim-sizing-1028`
  (MacBook only, not for master-mac).

## Next, in order

### 1. ORB amended_6 (row 62): skeptic rounds

- Proposal: `docs/studies/orb_in_play_amended_6_PROPOSAL.md`. Prereg: `docs/studies/orb_in_play_prereg.json`
  (amended_5 in force). Code: `tools/studies/orb_in_play.py`. Volume-only check:
  `tools/studies/orb_in_play_e_split_check.py`.
- Run **result-skeptic Mode 1** on the proposal. The reviewer brief is `.claude/agents/result-skeptic.md`; run it as a
  separate, read-only review session. Apply its text verbatim to the proposal, commit, then run round 2. Max two rounds.
- Then ask the operator for READY. Only after READY:
  1. Register the text as `amended_6` in the prereg JSON (verbatim, with the proposal's sha).
  2. Make the code change listed in the proposal, plus its tests.
  3. Get a code review.
  4. Count on the mini: `EEH_ROOT=$PWD .venv/bin/python tools/studies/orb_in_play.py --prereg-commit <sha>`.
  5. If `powered`, score with `--score --skeptic-ready <ref>`.
  6. Write a running-list row.
- Data is already fetched (`ai_reports/orb_in_play/` on the mini). The count + score take minutes. No refetch.

### 2. Entry-slip diagnostic (row 63): read-only, no prereg needed for the diagnostic itself

Split each fill's slip against the decision price into:

- (a) decision-price staleness (`features.ask` is a last print),
- (b) send latency (arm → fill),
- (c) the real spread crossed.

Use Alpaca fills and recorded quotes (reuse `tools/studies/marketable_limit_hist.py` matching). Report the shares and
bp. **Propose** fixes and do not ship any. A trading-path change needs the operator's OK and its own test.

### 3. Write down two unrecorded results (Grok's 10/31 list) as rows 64-65

- square_relax fidelity FAILED on the tape_only code (buy recall 0.955), so the long replays never ran.
- The IEX count is UNDERPOWERED: the delayed-spread gate left too few wide orders, so the later stages did not run.

Find the numbers in their `ai_reports/` outputs on the mini. Do not re-run them.

### 4. Engineering, needs the operator's OK to start

- The nightly exact replay hits the 6 GB cap for the 4th night. Diagnose what grows; propose chunking or a cap change.
- Find the test that rewrites `config/bot_config.json` and make it use a temp path.

Board ranking (row 53) cannot score before Monday 11/2 and needs `buy_qty` / `sell_qty` in the fill-matched table.
Spread compression / low-float (row 58) each need a prereg.
