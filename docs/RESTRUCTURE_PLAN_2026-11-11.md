# Restructure plan (2026-11-11)

Written for the operator and for any assistant (Claude, Gemini, Grok, Codex) that picks up a work package. Read sections 1-3 before
touching anything. Each work package in section 5 stands alone: goal, files, steps, definition of done.

## 1. Where the project stands

- The desk is a working paper day-trading platform on Alpaca: it finds names, seats them, arms, orders, manages and exits on its own,
  and its record matches the broker (114 of 114 orders).
- It has no edge. About 135 tests (docs/running_list.md, rows 1-117) put every intraday entry rule at roughly zero before cost on bar data:
  the live rule, earlier entries, longer intraday holds, cheap volatile names, a learned ranking model, a 5-name day hold, candle and
  support patterns, the reversal side, and a paired trade. The desk loses about its cost per trade (4-6 bp).
- What is worth keeping: execution and risk code, the broker-truth report, the pre-registration habit, and the CPU test tools built
  11/10-11/11 (section 4).

**The longer horizon was tested on 11/11 and also failed** (running list row 118). Six days-to-weeks rules on 3,639 names,
2017-2022: momentum, 52-week-high nearness, new 60-day highs, one-week losers, one-month momentum, and a ride-the-trend rule.
None beat the average tradeable name after adjusting for market exposure (t between -1.65 and +0.28), and every one returned less
per year than holding SPY (+11.3%). The ride-the-trend account made a median +3.3% a year, and anywhere from -5.9% to +19.5%
depending only on which ten names it happened to hold.

So the honest starting point for a restructure is this: **no rule tested at any horizon, from 3 minutes to several weeks, has beaten
holding the index.** The benchmark every future claim must beat is SPY bought and held over the same window.

## 2. The direction

There is no proven edge to build around, so the restructure is not "switch to strategy X". It is three things, in this order:

1. **Stop paying for the search.** Keep the desk on paper, at minimum cost, as plumbing. Do not add gates, flip config, or chase
   frequency targets ("an open every 10 minutes") until a rule has passed section 6.
2. **Make the search cheap, honest and different in kind.** Every price-only rule on bars has now been tried. New ideas must bring
   new information (order flow from trade ticks, real quotes, fundamentals or event data, or the operator's own judgment), and must
   go through the tools in section 4. Each test costs minutes of CPU, not weeks of waiting.
3. **Test the one edge with a real-money record: the operator's own picks.** His two large gains came from choosing a company and
   holding it for months. Two wins in a rising market prove nothing, but it is cheap to measure: log every pick before the outcome
   and score it against SPY and against random names (WP4). If his picks beat the control, the platform's job becomes managing
   entries, size and exits for those picks. If they do not, that question is closed too.

A stop date belongs in the plan: see WP9.

## 3. Rules every assistant follows

1. **Write the test down first.** A new idea gets `docs/studies/<name>_prereg.json` (rule, data, pass line, fail line) committed
   BEFORE its script runs on data. One read. A changed rule is a new idea and needs data that has not been looked at.
2. **Read results per trade.** The mean of daily means once called a losing rule profitable (running list rows 116-117).
   `tools/studies/harness.py` prints a `PER TRADE` line; quote that one. For rules that fire in bursts, also simulate the position
   limit in time order (`tools/studies/pair_trade.py` shows how).
3. **Judge against a control, in both halves by date,** with the t computed across days (or cohorts), never across trades.
4. **A positive result goes to the `result-skeptic` reviewer before it reaches config, live trading or the operator as "found".**
5. **No config change without evidence.** A commit that touches `config/bot_config.json` needs an `Evidence:` line (a report path, or
   `operator: <reason>`). `scripts/check_config_evidence.py` enforces it; run `scripts/install_hooks.sh` once per checkout.
6. **Never `git add -A`.** The full pytest run rewrites `config/bot_config.json`; restore it with
   `git checkout -- config/bot_config.json`. Stage files by name.
7. **The live desk runs on the Mac mini** (`ssh mac-mini` on the LAN, `ssh mac-mini-away` over Tailscale). Restart it only from the
   mini's own Terminal and only before 09:30 ET: a mid-session restart wipes indicator history. The mini cannot `git push`.
8. **Heavy jobs on the mini run at `nice -n 19`,** and anything that calls Alpaca's trading-account data runs outside 09:00-16:30 ET.
   Studies use the data-only key in `secrets_data.json`.
9. **Say what failed.** Record every result, pass or fail, as a row in `docs/running_list.md`. Do not re-test a closed idea without
   a new data source; check `docs/studies/` and the running list first.
10. **Paper only.** Nothing in this plan authorizes a live-money order. Going live is the operator's decision, after section 6.

## 4. Tools that already exist (use these, do not rebuild them)

| Tool | What it does |
|---|---|
| `tools/studies/harness.py` | Any intraday entry rule -> verdict in seconds: lift vs same-minute peers, per-trade net, look-ahead guard, hold-out, canaries. Signal files live in `tools/studies/signals/`. |
| `tools/studies/pattern_battery.py`, `entry_ideas.py` | Run a dictionary of rules through the harness in one table. |
| `tools/studies/reversal_map.py` | Maps forward return by a feature's level (dose-response), by day. |
| `tools/studies/pair_trade.py` | Per-trade and slot-limited simulation with a hedge leg. |
| `tools/studies/ml_rank.py`, `day_pick.py` | Learned ranking on 25 causal bar features; 5 picks held to the close. |
| `tools/studies/daily_horizon.py` | Days-to-weeks rules on ten years of daily bars: cohort baskets and a ride-the-trend rule with a 10-position account. |
| `tools/trade_sim.py`, `tools/sweep_replays.py` | The desk's real exit code on recorded prints; config variants on recorded sessions. |
| `tools/broker_truth.py`, `tools/daily_scoreboard.py` | What the account actually did, from Alpaca fills only. |

Data on the mini, under `ai_reports/warehouse/`: `sip` (400 liquid names, 1-minute, 4/27-10/15/2026), `sip_fresh` (10/16-11/10),
`sip_small` ($2-20 volatile names), `sip_top` (60 largest names, 2024-01 to 2026-04), `sip_hedge` (SPY, QQQ), and
`ai_reports/gapdown/daily.pkl` (daily open/close, 3,639 names, 2016-2026).

## 5. Work packages

Order: WP1-WP3 are engineering and can start now, in parallel, by any assistant. WP4-WP7 are the search. WP8-WP9 are the operator's.

### WP1. Per-trade numbers on the nightly reports
- **Goal:** the desk's own reports cannot repeat the day-mean mistake.
- **Files:** `tools/daily_scoreboard.py`, `tools/sweep_replays.py`, `tools/trade_sim.py`, `scripts/nightly.sh`.
- **Steps:** add a trade-weighted net bp line (and halves) beside every day-level figure; in `sweep_replays`, print trade-weighted
  delta beside the day delta; add tests with a "few big winners on quiet days, many small losers on busy days" fixture
  (see `test_profitable_needs_per_trade_net_not_only_day_means` in `tests/test_study_harness.py`).
- **Done when:** `docs/SCOREBOARD.md` and a sweep `REPORT.md` both show per-trade net, tests pass, no change to any verdict rule.

### WP2. Nightly warehouse append
- **Goal:** every session's bars land in the warehouses automatically, so tests get stronger each day.
- **Files:** `tools/studies/warehouse_fetch.py`, `warehouse_small.py`, `warehouse_top.py`, `scripts/nightly.sh`; the liquidity table
  `ai_reports/gapdown/daily.pkl` ends 10/15 and needs extending (see `tools/studies/gapdown_short.py fetch_daily / repair_daily`).
- **Steps:** extend the daily table to the last session; append the day to `sip`, `sip_small`, `sip_top`, `sip_hedge`; run after
  16:30 ET at `nice 19`; write a one-line log; refuse to overwrite an existing day file. Fold `sip_fresh` into `sip`.
- **Done when:** three consecutive nights append without a manual step and `harness.py --selftest` passes on the new days.
- **Also:** nine FAILED fetch lines are recorded in `daily.pkl["log"]`, and delisted names are nearly absent for 2023-2025.
  Find out why (Alpaca inactive-asset coverage?) and state in the README whether it can be repaired.

### WP3. Take study code out of the live buy path
- **Goal:** `should_arm_buy` in `ai_entry_watch.py` (461 lines, 51 refusal exits) contains only rules that are live.
- **Needs first:** the operator's list of which studies are closed. Do not remove a control that a running forward test reads.
- **Steps:** list the 28 study / random-control / observe knobs (`grep -o 'ai_[a-z0-9_]*' ai_entry_watch.py ai_positions.py | sort -u`,
  filter on rand|shadow|observe|_test|_log); for each closed one, delete the code and its tests in its own commit; run the full suite
  each time (`scripts/regression_check.sh`); then move the 243 code-default knobs that are not in `config/bot_config.json` into
  `config.py` defaults with one line of documentation each.
- **Done when:** the regression check is green, a replay of three recorded days is trade-for-trade identical before and after
  (`tools/sweep_replays.py` with an empty variant), and the file is shorter.

### WP4. Operator-pick ledger (the one untested source of edge)
- **STATUS 11/11: BUILT.** `tools/pick_ledger.py`, prereg and skeptic fixes are in (running list row 119). What remains is the operator recording picks: `python3 tools/pick_ledger.py add SYMBOL --thesis "..." --months N --sell-if "..."` on the MacBook, then `git push`. Score on the mini with `.venv/bin/python tools/pick_ledger.py score`.
- **Goal:** measure whether the operator's own stock picks beat a fair control, before any money rides on them.
- **Files:** new `tools/pick_ledger.py`; existing `tools/studies/my_calls_score.py` (intraday "Buy?" clicks) as a pattern;
  `tools/studies/daily_horizon.py` for the control construction (`span_ret`, ratio lift).
- **Steps:** a command the operator runs to record a pick BEFORE the outcome: symbol, date, thesis in one line, intended hold
  (weeks or months), and what would make him sell. The entry is the next session's open. Score each pick at 1, 3 and 6 months as
  the ratio lift against (a) SPY and (b) the equal-weight return of all tradeable names over the same days. Picks named after the
  fact are listed and never scored. Write `docs/studies/pick_ledger_prereg.json` first: one read at 30 picks with a closed
  3-month window, pass = mean ratio lift vs SPY > 0 with a t across picks >= 2 and more than half the picks ahead of SPY.
- **Done when:** the ledger command exists with tests, the prereg is committed, and the first pick is logged.
- **Honest note:** this takes months to read. That is the real time scale of the only kind of trade the operator has made money on.

### WP5. Real quotes at the moment of a trade
- **Goal:** replace assumed costs (6 bp, "one cent") with measured ones, per name and per kind of minute.
- **Files:** `tools/studies/etf_cost_sample.py` and `iex_order_spread_calibration.py` show the quote-sampling pattern.
- **Steps:** for a seeded sample of name-minutes from `sip`, `sip_top` and `sip_small` (calm minutes, and minutes 0.5 prior-day ranges
  from VWAP), pull Alpaca historical SIP quotes for the 5 seconds around the bar open; store the quoted spread in bp. Give
  `harness.py` an option to charge the sampled spread by name and by minute type instead of a flat cost.
- **Done when:** a table of median and 90th-percentile spread by universe and minute type is in `docs/`, and the harness can use it.

### WP6. Ideas that bring new information (the search itself)
Each is one prereg, one script on the harness, one skeptic review, one read. Do them one at a time. Expect most to fail.
1. **Order flow from trade ticks on the lowest-cost names.** Earlier work found flow real but worth 1.5-2 bp against a 6 bp cost.
   On the 30 largest names the cost is about 1 bp. Use Alpaca historical trades (free) to build signed flow per minute and test it
   with `--top-n 30 --cost-bp 1`. Needs WP5 for the true cost.
2. **Earnings and guidance events with the size of the surprise.** Price-only earnings drift failed (10/07). The untested part is
   the surprise itself, which needs a data source the project does not have. First task: find a free or cheap source and say what
   it costs; do not build until the operator approves the spend.
3. **SPY at market extremes, in shadow.** Buying SPY at the open after a 1.5% drop, three down closes, or a 1% gap down made
   +13 to +19 bp to the close over ten years, positive in both halves, but t 1.3-1.5 (not significant) and about 20 events a year.
   Log it forward in shadow (WP7); do not trade it.
4. **Anything the operator sees on a chart.** Write it as a signal file (ten lines, see `tools/studies/signals/full_green.py`) and run it.
   The answer takes seconds. This replaces arguing about ideas.

Closed, do not repeat without new data: everything in section 1, plus the list in `docs/EDGE_SEARCH_MAP_2026-11-06.md`.

### WP7. Shadow logger for the SPY-extremes trade
- **Goal:** collect forward events for WP6 item 3 at zero risk.
- **Steps:** a nightly check that, when one of the four conditions (drop of 1.5%+, three down closes, 1%+ gap down, gap down after a drop; `docs/running_list.md` row 115) holds at the close,
  writes a row; the next night it fills in SPY's open-to-close return. Prereg: one read at 40 forward events.
- **Done when:** the logger runs from `scripts/nightly.sh` and has written its first row.

### WP8. What the desk does while the search runs (operator decides)
- **Option A, keep trading on paper as now.** Costs nothing real; keeps the plumbing exercised; produces about 30 more data points a
  day that have not changed any conclusion in two months.
- **Option B, one slot, small size, mornings only.** Same plumbing check, less noise in the reports.
- **Option C, pause entries.** Keep the book, data and monitoring running; arm nothing.
- No assistant changes this. It is a config commit with `Evidence: operator: <reason>`.

### WP9. A stop date (operator decides)
- **Why:** six months of search have produced no edge. A plan with no end keeps spending time and model usage on the same result.
- **Proposal:** on a date the operator picks (the 11/06 edge map already named about 12/1), review: (1) has anything in WP6 passed
  section 6 items 1-2? (2) what does the pick ledger show so far? If the answer to (1) is no, the systematic search parks and the
  platform is kept only for WP4.
- **Done when:** the date and the two questions are written at the top of `docs/running_list.md`.

## 6. What "ready for real money" means

Nothing here is ready. A rule is a candidate for the operator's go/no-go only when ALL of these hold:

1. It passed a pre-registered test on data it had never seen, per trade, in both halves, after cost.
2. The skeptic reviewer signed off on that result.
3. It ran on paper for a pre-stated period and count, and the paper result sits inside the range the test predicted.
4. The cost in the test was checked against real quotes at the moments it trades.
5. The worst drawdown in the test is one the operator has said, in advance and in dollars, he will sit through.

The operator decides. No assistant places or enables a live order.
