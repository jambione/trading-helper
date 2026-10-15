# SIP month plan (operator 2026-10-15)

The operator will pay for one month of real-time SIP next month to test the most we can, including premarket. Goal: spend the month running tests, not building switches. Everything below is ready **before** the subscription starts.

Supersedes the funding gate in `docs/SIP_FROM_250_PLAN.md` (9/20), which tied SIP to the $250 live account earning $99 first.

## 1. What we are buying (verify on Alpaca's pricing page before paying)

| | Free (today) | Algo Trader Plus (~$99/month) |
|---|---|---|
| Quotes / trades / bars | IEX only real time (~2-3% of volume); SIP 15 min late | **SIP real time** (every US exchange) |
| Premarket / after-hours prints | IEX only (very sparse) | SIP, all venues |
| Data API rate limit | 200 calls/min | ~10,000 calls/min |
| Streaming symbols | limited (we use Finnhub, 50-symbol cap) | full-market SIP stream |

Not included: order-book depth (Level 2). Depth-dependent ideas stay untestable.

## 2. What we already know SIP is worth (free historical SIP, simulated)

| Signal | Delayed / IEX | Real-time SIP (simulated) |
|---|---:|---:|
| News headline, 1 s after publication | — | +4 bp gross |
| Sector lead-lag | −12 bp net | −11.6 bp net |
| Volume breakout | ~0 gross | +9 bp gross, fails after cost |
| %R + RSI confluence | +0.5 bp | +4 bp |
| Momentum entries, perfect data (9/25) | — | −15 bp net |

So far real-time SIP adds about 0-5 bp on these signals. The month should therefore target what free data **cannot** show at all: premarket, sub-minute stop/exit execution, staleness refusals, and the runner tail.

## 3. Code switches to have ready (inventory 10/15)

Feed choice is not one knob today. Each live touchpoint, and the change needed:

| Where | Today | Needed for SIP |
|---|---|---|
| `alpaca_api._get_feed_arg` (`alpaca_bar_feed`) | `iex` | set `sip` (exists) |
| `signal_engine.py:685` (`ALPACA_BAR_FEED` env) | `iex` | env `sip` (exists; env, not bot_config) |
| `alpaca_price_poll.py:118` | **hard-coded IEX** | read `alpaca_bar_feed` |
| `ai_trading.py:299/482/503` latest quote/trade | **hard-coded IEX** | read `alpaca_bar_feed` |
| `ai_positions.py:1246` latest quote | **hard-coded IEX** | read `alpaca_bar_feed` |
| `movers_screener.py:271/368`, `tight_screener.py:408` snapshots | **hard-coded IEX** | read `alpaca_bar_feed` |
| `ai_entry_watch.sip_spread_pct` / gap (`delay_min` 16) | 16-min-old SIP quotes | `delay_min` 0 via a knob |
| `movers_screener` / `tight_screener` `ai_movers_sip_delay_min` 15 | pacing + gates wait for 09:46 | 0-1 |
| Stale-tape ceilings (`ai_watch_decision_max_age_sec` 15, `ai_watch_tape_gap_mult` 1.5) | tuned for IEX gaps | re-tune (SIP prints every 2-9 s) |
| Book tick (`ai_book_tick_sec` 3) | trading-API limit | unchanged (trading limit, not data) |
| Phase B premarket lane (`phase_b.py`) | dry, every arm refuses `phase_b_missing_print` | live on SIP prints |
| Extended-hours orders | — | limit orders only, `extended_hours=True` (Alpaca rule) |

Work item **S1** (build now, test on paper with the knob still `iex`): one `alpaca_bar_feed` knob read by every row above, and a `sip_realtime` knob that sets the delay terms to 0. Default unchanged; flipping the two knobs is the whole switch.

## 4. Tests for the month (each pre-registered and SIMULATED on historical SIP before the month starts; only sim survivors run live)

| # | Test | Why it needs real-time SIP | Simulate now with |
|---|---|---|---|
| T1 | **Premarket entries (Phase B)** on gappers, 04:00-09:28, limit orders | IEX premarket is nearly empty | historical SIP premarket bars (five-pillars premarket data showed spikes and fades like RTH - prior is low) |
| T2 | **Same desk, SIP vs IEX signals**, paired days | the desk's %R/arm reads IEX | hist simulator with `--engine-bars sip` |
| T3 | **Stale-tape refusals disappear** | 19% of arm refusals were IEX gaps | count on recorded days: refusals whose name printed on SIP |
| T4 | **Runner + entry filter** with real-time stops | the tail and the stop are sub-minute | right_tail_runner on SIP (already SIP-priced) |
| T5 | **Real-time spread gate** (no 16-min lag) | cost is ~the spread | replay with spreads at decision time vs 16 min old |
| T6 | **Opening 15 minutes** (09:30-09:45, currently skipped) | free SIP pacing is blind until 09:46 | SIP minute bars 09:30-09:45 |

## 5. Month schedule

- **Before subscribing:** S1 built and merged with defaults off; T1-T6 pre-registered and simulated; only the passing ones get a live slot.
- **Day 1:** flip the two knobs; restart; confirm every touchpoint reports `sip` (a startup check in the log).
- **Week 1:** IEX and SIP side by side where possible (shadow), plumbing checks, Phase B paper fills.
- **Weeks 2-4:** the surviving tests, each with its fixed read date.
- **End of month:** keep or cancel on one number - measured improvement in net $/day on paper versus the $99 cost, plus whatever premarket shows.

## 6. Decisions for the operator

1. Confirm the plan and the price on Alpaca's page.
2. Approve S1 now (code only, defaults off - no live change until the month).
3. Start month: earliest after S1 and the T-sims are done (about 1-2 weeks of evenings).
