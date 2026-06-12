# Trading Engine — 3-Indicator Strategy Runbook

The engine automates the discretionary TradingView workflow: the Discord OCR
feed surfaces candidate tickers (the catalyst), and the 3-indicator strategy
(`strategy_three_indicator.py`) times entries and exits. Orders route through
`alpaca_trader.py` under `TRADER_MODE` (off / paper / live).

## The signal (what fires a BUY / SELL)

**BUY** — all three within the confirm window (default 8 bars, no lookahead):
1. **MACD** (long indicator): bullish cross within the window, MACD line still
   above signal now, and the histogram is *wide* — `|hist| ≥ macd_sep_mult ×
   rolling std(hist)` (scale-free "the gap looks big for this stock")
2. **CM RSI-2** (short/fast trigger): was below `cm_rsi_buy_max` (default 40)
   and rising at some bar in the window
3. **%R Trend Exhaustion** (long indicator): fast %R rising toward 0 at some
   bar in the window

**SELL** — momentum reversal, `exit_mode=any` exits on the FIRST of:
1. Bearish MACD cross within the window
2. CM RSI-2 above `cm_rsi_sell_min` (default 90) and falling
3. %R rolling over: was above `rte_sell_from` (default −10, near 0) and falling

Plus the engine's own backstops, whichever fires first (checked every second
against the live Finnhub price):
- `TAKE_PROFIT` % — the small-consistent-profit scalp target
- `STOP_LOSS` % — the tight protective stop
- `MAX_HOLD_MINUTES` — time stop

## Running it from the dashboard

The **Trading Engine panel** (visible only to the owner, between the donate bar
and the main grid) is the day-to-day control surface:

- **Status chips** (live): trader mode, strategy, engine freshness, kill-switch,
  today's realized P&L, trades today, PDT counter, open positions + exposure
- **⚙ Settings**: edits whitelisted signal_engine.env keys (mode, scalp exits,
  risk guard, 3-indicator params). "Save + Restart Engine" applies them.
  `TRADER_MODE=live` is deliberately NOT settable here — file edit only.
- **📊 Report**: per-day P&L, green-day rate, exit-reason breakdown
- **⟳ Restart**: re-execs the engine (open positions are re-adopted on boot)

Security: settings writes and restarts work from the local dashboard without
ceremony; through the public URL they require `engine_control_secret` (set it
in config/bot_config.json — the panel prompts once and remembers it).

## Workflow

### Stage 1 — Validate the port vs TradingView (TRADER_MODE=off)
1. `signal_engine.env`: `STRATEGY_MODE=three_indicator`, `REALTIME_BARS=1`,
   `TRADER_MODE=off`, `COMPARE_TICKERS=<symbols you have open on TV>`
2. Sync `THREE_IND_*` params to your TV chart settings (see signal_engine.env)
3. Run `python start_all.py`; watch the dashboard CM / %R / MACD pills next to
   the TradingView chart for a few sessions
4. Review `signal_log.json` BUY/SELL entries against the chart after each session
5. Don't advance until the pills and signals agree with what you see on TV

### Stage 2 — Paper trade (TRADER_MODE=paper)
1. Set the scalp recipe: `TAKE_PROFIT` (e.g. 2.0), `STOP_LOSS` (e.g. 1.0),
   `TRADE_AMOUNT`, `MAX_TOTAL_EXPOSURE` (positions cap = exposure ÷ amount)
2. Set `DAILY_LOSS_LIMIT` (kill switch) — exits always still fire
3. Review daily: `venv/bin/python tools/paper_report.py --daily --size 500`
   - Green-day rate, win rate, exit-reason breakdown (3ind_reversal vs
     TAKE PROFIT vs STOP LOSS)
4. The engine survives restarts: open Alpaca positions are re-adopted at boot
   and reconciled every 60s; entry prices rebase to actual fills

### Stage 3 — Go live (TRADER_MODE=live)
Checklist before flipping:
- [ ] N consecutive green paper days/weeks at the target size (your call on N)
- [ ] `PDT_PROTECT=block` if the account is a margin account under $25k
- [ ] `DAILY_LOSS_LIMIT` set to a loss you can shrug off
- [ ] `MAX_TOTAL_EXPOSURE` set
- [ ] Alpaca LIVE keys in signal_engine.env (paper keys are different!)

### Compounding
Sizing is fixed dollars per trade (`TRADE_AMOUNT`). To compound: when
`paper_report.py --daily` shows consistent green at the current size, raise
`TRADE_AMOUNT` (and `MAX_TOTAL_EXPOSURE` proportionally), then keep watching
the daily report at the new size.

## Risk layer (trade_guard.py)
| Control | Env key | Behavior |
|---|---|---|
| Daily loss kill switch | `DAILY_LOSS_LIMIT` | Realized loss ≥ limit → no new buys until next ET day |
| Trades/day cap | `MAX_TRADES_PER_DAY` | Blocks buys past the cap |
| PDT protection | `PDT_PROTECT` | warn/block at 3 day-trades per 5 business days |
| Exposure cap | `MAX_TOTAL_EXPOSURE` | Max concurrent $ at risk |

Guard state persists in `trade_guard_state.json` (restart-proof) and is
surfaced in `signal_state.json` under `risk` for the dashboard.
