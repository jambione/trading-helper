# Claude brief: weekly undervalued trending book

Written Thursday 2026-10-15, before the open. Paste this whole file into Claude.

You are changing the trading-helper overnight paper book into a weekly book. Work in the MacBook repo `~/repo/trading-helper` on branch `master-mac`. The running book is on the Mac mini (`ssh mac-mini-away`, same repo path, `.venv/bin/python`). Always end an ssh command with `< /dev/null`.

The operator has already decided the rules below. Do not reopen them, do not tune them on history, and do not run a backtest to pick new cutoffs.

## What this book is

Twenty names, held from one week's close to the next. A name has to be a liquid common stock, cheap on trailing earnings, and already in an uptrend. AGY and Grok both have to rank it. The code places the orders. Neither model trades.

The first buy is **Friday 2026-10-16 at the close**, on the existing overnight **paper** account, after Thursday's open has flattened the old 12-1 book. Scoring starts at that Friday close. Thursday's flatten is the old book ending. It is not week 1.

## Clock, and the one window you may touch the mini

Verify the New York clock before every mini action. As of this brief the old paper book is still long the Wednesday 2026-10-14 close buy (20 names, about $443 cash). The live overnight account is flat at about $98, its arm file is absent, and it must stay that way.

Thursday 2026-10-15, old schedule, leave it alone:

| ET | What the old agent does |
|---|---|
| 09:31 | Market-sell the 20 names |
| 09:33 | Top-up sell of anything still held |
| 09:50 | Reconcile |

**Do not deploy, pull, restart, bootout, or kickstart anything on the mini before that reconcile shows holding 0.** A restart at 09:31 cuts the sell off while orders are out. Writing and testing code on the MacBook before 09:31 is fine.

**Deploy window: after the 09:50 reconcile confirms holding 0, and before 15:40 ET.** The 15:58 step is another 12-1 buy. If the old agent is still loaded then, it spends the cash you just raised.

Deploy sequence, only inside that window:

1. Read `ai_reports/overnight/snapshot.json` on the mini. Proceed only if `holding` is 0 and cash is about equal to equity.
2. `launchctl bootout gui/$(id -u)/com.jambi.overnight-book` so the old process cannot reach 15:58. Run that on the mini. Do not bootout `com.jambi.month-end-book`. Do not bootout `com.jambi.trading-desk`. Leave `com.jambi.overnight-live` running only if you have confirmed it is disarmed (`config/overnight_live.armed` absent). Do not create that file.
3. Push from the MacBook and `git pull --ff-only` on the mini. If the pull refuses because the tree is dirty, stop. Do not stash, reset, or checkout over local files. `valid_tickers.txt` is a known dirty tracked file. Do not revert it by hand and do not commit it.
4. `launchctl kickstart -k gui/$(id -u)/com.jambi.overnight-book` on the mini. Never `./trading restart` over ssh. Never pytest on the mini.
5. Read the new log line. It must say the process is the weekly book, holding 0, and the next order is Friday's close. If it still plans a 12-1 buy for Thursday, boot it back out and stop.

If you miss the window and the 15:58 buy has already filled, stop. Tell the operator. Do not cancel those positions yourself and do not put the weekly buy on top of them. Those shares have to be sold by the old Friday-morning sell before any weekly buy.

## Hard rules

- The repo is public. Never commit, print, or log API keys, tokens, account numbers, or Keychain contents.
- Paper only. Do not arm live. Do not set `OVERNIGHT_ACCOUNT=live`. Do not write `config/overnight_live.armed`. Eight scored weeks on paper before anyone discusses live money.
- Do not change the day desk's orders, gates, or config. `ai_trader.py` already publishes `ai_reports/overnight/snapshot.json`. Leave that wire alone.
- Leave `com.jambi.month-end-book` and its account alone. It is a separate paper account.
- Tests run on the MacBook only. After any suite, `git checkout -- config/bot_config.json` if a test dirtied it. Never commit that file.
- Do not stage or edit `tests/test_pctr_line_probe.py` or `tools/pctr_line_probe.py`. Stage by explicit path. Never `git add -A` or `git add .`.
- Do not commit unless the operator asks.
- Keep `rank()` and the old night ledger. `tools/studies/overnight_plan_replay.py` replays `rank()`. Append new weeks to a new file, `ai_reports/overnight/weeks.jsonl`. Do not rewrite `nights.jsonl`.
- If AGY and Grok do not produce an agreed file before 15:40 on an order day, buy nothing. Never fall back to 12-1 momentum.

## Read these first

- `overnight_book.py` — the scheduler, `plan()`, `rank()`, `buy()`, `sell()`, `sell_catchup()`, `schedule()`, `build_snapshot()`. Paper is always armed. `sell_catchup()` market-sells anything still held between the last sell step and the first buy step. That catch-up is why a weekly hold cannot keep the current clock.
- `static/js/feeds.js` `_paintOvernight` and the overnight block in `dashboard.html`. The strip reads the snapshot. Change its words. Keep the same DOM hooks.
- `ai_suggest.py` `call_agy_cli` and `call_grok_cli`. Reuse them. Do not add an API client or a new login.
- `seed_rank.py` — agreement means a name both models list. The day desk's publish mode is union. This book does not use that path and does not write the day-desk watchlist.
- `swing_screener.py` `add_fundamentals` — trailing P/E is Finnhub `stock/metric` field `peTTM` (fallback `peBasicExclExtraTTM`).
- `ticker_filters.py` `is_common` / `is_levered_etp`, and `FUNDISH` in `overnight_book.py`.

## The screen, frozen before either model runs

One file per session, `ai_reports/overnight/weekly_screen_YYYY-MM-DD.json`, built from data the code fetches. Models may not supply prices, P/E, or membership.

A name is on the screen only when all of these hold:

- US common stock on NYSE, Nasdaq, or AMEX. Use the existing universe filter: `is_common`, not a leveraged or inverse product, and not `FUNDISH`.
- Last close in the plan's bars is **>= $5 and < $50**.
- Average daily dollar volume over 20 sessions is **>= $20 million**, with at least 15 of those sessions present. This replaces the old $50 million floor for this book. Today's dollar volume does not qualify a name.
- Finnhub trailing P/E (`peTTM`, else `peBasicExclExtraTTM`) is **> 0 and <= 12**. Missing, null, or non-positive P/E is out. Do not use forward P/E. Do not let a model fill the number.
- Uptrend, on the same adjusted daily closes the plan already uses, sessions not calendar days:
  - last close above the 50-session average,
  - last close above the close 60 sessions ago,
  - last close above the close 20 sessions ago.
- At order time only, the spread is within **0.20% of the midpoint**, using the desk's existing SIP quote path. A name that fails this is skipped for that order. It stays on the agreed list.

Do not require a 10% day move, relative volume, or a Stocktwits rank. Those are the movers seed. Hot mover names fade over 1 to 10 days. Today's percent change may be stored on the row. It does not qualify or rank.

## How AGY and Grok pick

On each weekday, after the screen file exists, both models get the same file and nothing else they are allowed to trade from.

Each returns a ranked 30, JSON only:

```json
{"picks": [{"sym": "ABC", "rank": 1, "tag": "none", "why": "one sentence"}]}
```

`tag` is one of `none`, `cycle-peak`, `falling`, `dilution`. Reject a response that is not that shape, and reject any symbol that is not on the frozen screen.

A name is eligible only when both lists contain it. Sort eligible names by the sum of the two ranks, low sum first. The book is the first 20. If they agree on fewer than 20, the book is that short list. Do not pad from one model. Log the one-model names and do not buy them.

Write `ai_reports/overnight/weekly_agreed_YYYY-MM-DD.json` and point that day's `plan_YYYY-MM-DD.json` `picks` at the agreed names, in that order. `buy()` already reads `plan_DAY.json`. Keep that.

The order day is the last NYSE session of the week (Friday, or Thursday when Friday is a holiday). The agreed file from that morning is what the close buys. A later rescore the same day does not change the order.

## Orders

Reuse `buy()` sizing: 98% of cash (`EQUITY_FRAC`), split equally across the names that will actually be bought, whole shares, top-up, reconcile. Paper stays on market orders. Do not switch paper to auction.

Order day at the close:

- Sell names that are held and are not on today's agreed list.
- Buy names that are on today's agreed list and are not held.
- Keep the overlap. Do not sell it and buy it back.

Every other session:

- Rebuild the screen, ask both models, write the agreed file, and refresh the snapshot with who would leave and who would enter on the next order day.
- No orders.
- `sell_catchup()` must not run. A hold that is still in the agreed list is not a missed sell.

The first order day, Friday 2026-10-16, the account is flat, so the buy is the whole agreed list.

If the spread check or a failed model call leaves nothing to do, do nothing. Log why.

## Score

Append one row to `ai_reports/overnight/weeks.jsonl` at the close of each order day after the first. The week is prior order-day close to this order-day close.

Charge **4 bp** for each name that entered at the start of the week and **4 bp** for each name that leaves at the end. A name held across the week pays nothing that week. This charge is on the score only. Do not send it to the broker.

Also write, and do not trade, the equal-weight return of the mechanical screen (every name that passed the screen, models ignored) over the same week. The snapshot shows both.

Until the first scored week, the strip says no scored weeks yet. Remove the 16.1 bp/night backtest figure from this strip.

## Dashboard

Same Overnight tab, same snapshot. Change the visible words:

- Label: `Weekly book` plus the date of the held book.
- Score line: weeks, mean bp per week, dollars, green weeks. No "bp/night". No backtest 16 bp.
- Status: holding count, next order (Friday close, or "no order today"), and on non-order days the names that would enter or leave.
- Rows: symbol, whether it is held, the two ranks, the trap tag.

Do not add a second panel.

## Tests, on the MacBook

Pure tests, no network, no broker:

- Screen accepts a fixture that passes every rule and rejects each rule alone: price $4.99, price $50, ADV under $20 million, missing P/E, P/E 0, P/E 13, below the 50-session average, down over 60 sessions, down over 20 sessions, warrant, fund.
- Agreement: intersection ranked by sum of ranks, cap 20, no padding, unknown symbols dropped, bad JSON is an empty book.
- Schedule: Tuesday, Wednesday, and Thursday have no sell, no buy, and no catch-up. The last session of the week has a close sell of the names that left and a close buy of the names that entered. A held name that is still agreed is not in the sell.
- A holiday Friday makes Thursday the order day. Use a fixed calendar in the test, not the live clock.
- Old `rank()` still returns 12-1 order for a fixture. Do not change that function's meaning.

## Done

MacBook tests for the new behavior pass. The mini, inside the window above, is running the weekly scheduler, holding 0, with the next order on Friday 2026-10-16 at the close. The day desk and the month-end agent were not restarted. Live is still disarmed. Report the agreed-list path, the test command, and the mini log line that shows the next order. If any of the window checks fail, stop and report that instead of improvising.
