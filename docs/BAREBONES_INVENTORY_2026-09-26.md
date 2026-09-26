# Barebones inventory — 2026-09-26

**Purpose.** Prep for the barebones discussion. The goal is performance, not
tidiness: more good entries taken on time (concurrency, cadence), without
making net P/L per trade worse. Simplicity is the means. Every item below is
judged by what it does to opens and profit, using the 9/25 recordings.

This is a read-only survey: nothing here changes code or config. Proposals are
marked **keep / replace / delete / fix**, for discussion.

## Headline numbers

| | Now |
|---|---|
| Settings (config.py defaults ∪ bot_config.json) | **614**, of which `ai_watch_*` = 218 |
| Settings switched off (0 / False / empty) | 102 |
| Settings with no reader outside config.py | 2 (`cm_rsi_prefer_green`, `rsi_sell`) |
| Decision code | ai_entry_watch.py 18,375 lines / 375 functions; ai_positions.py 7,311; phase_b.py 1,613; seed_rank.py 927 |
| Live opens 9/25 | 20 all day; replay of the same code 3-4× that |

## What actually costs opens (9/25 evidence, largest first)

| # | Layer | Evidence | Kind |
|---|---|---|---|
| 1 | **Stale price data** | 43% of all RTH arm checks refused `tape_only`; +839 `mid_rise_stale`. Of the replay's extra opens, 28 of 48 were names live refused as stale. At admission: `no_tape` 132 names, `stale_tape_admit` 65, `no_price` 31. | plumbing, not strategy |
| 2 | Paint-latch bug | 7 entries in 4 h that live's own %R crossed and never armed (RKLB 12:47:50). **Fixed 24cc6bf** (deployed 9/25 17:13). | bug, done |
| 3 | Book drift / many intake paths | 4 of 5 missed live buys were names the replay's book never seated; six seed paths + soft seed + morning flood + day roster feed one book. | complexity |
| 4 | Strategy gates | thin_rvol (148 names at seed), spread_wide 59, below_min_price 41, admit_range_pos 35, not_uptrend 60, float_too_big 16, gapped_down 21. | intended |

The largest lever is (1), and it is not a strategy question. Its known causes:
- **The 15 s last-trade rule on IEX-only prints** marks liquid names stale 16–63% of the time, while SIP shows trades every 2–9 s ([[iex-trade-gaps-are-not-staleness]]).
- **Alpaca request pressure from the dashboard:** on 9/25 in RTH there were 5,923 L2 batch-quote calls and 4,374 latest-trade calls, and the engine hit 280 `fetch_bars` 429s. The L2 panel alone is ~106 req/min for a UI panel ([[l2-panel-eats-half-the-alpaca-budget]]).

## Inventory

### Intake (what gets nominated)

| Item | Where | Keys | Status | Proposal | Why |
|---|---|---|---|---|---|
| Trending seed | ai_entry_watch.sync_watch_from_source_panels | `ai_watch_seed_trending(_n)` | live, n=20 | replace → book server input | one intake path |
| Momentum seed (+ open variant) | same | `ai_watch_seed_momentum*` (4) | live, n=12/14 | replace → book server input | momentum carries the loss ([[loss-concentrates-in-the-momentum-source]]) |
| Movers seed | same | `ai_watch_seed_movers(_n)` | live, n=25 | replace → book server input | |
| Research seed (AGY/xAI boards, seed_rank) | same + seed_rank.py (in ai_trader) | `ai_watch_seed_research*`, `ai_seed_rank_*` (6) | live | keep as book server input | |
| Soft seed (continuous) | same | `ai_watch_soft_seed_*` (7) | live, 120 s | delete | a second intake loop into the same book |
| Morning flood 09:30–11:00 | same | `ai_watch_morning_flood_*` (4) | live | delete | seats everything, then eviction fights it |
| Day roster | same | `ai_watch_day_roster` | live | delete | extra book state the replay could not rebuild |
| Book server | book_server.py (362 lines) | `ai_book_server_mode=shadow`, `_max_seats=12` | shadow | **replace all of the above with this** | one ranking, one book |
| Phase B premarket | phase_b.py (1,613 lines) | `ai_phase_b_*` (27) | enabled; 100% refuse `phase_b_missing_print` | switch off, keep the ledger | blind on IEX premarket ([[premarket-shelved-until-sip]], [[phase-b-dry-lane-is-mute]]; the user wants the ledger kept) |

### Admission / book (who holds a seat)

| Item | Keys | Status | Proposal | Why |
|---|---|---|---|---|
| Inclusion gates: price band $20–100, float ≤800M, uptrend, rvol ≥1, SIP spread ≤0.20%, gap-down >1% | `ai_watch_min_price`, `ai_max_price`, `ai_watch_max_float_m`, `ai_watch_require_uptrend`, `ai_watch_min_rvol`, `ai_watch_max_sip_spread_pct`, `ai_watch_gap_down_block_pct` | live | **keep** (intended safety) | |
| Momentum spread exemption | `ai_watch_momentum_spread_exempt=True` | live | fix (turn off) — test first | let 80–100 bp names past the spread gate ([[momentum-spread-exempt-leaks-wide-names]]) |
| Admit range position ≤90 | `ai_watch_admit_max_range_pos` | live | keep, but fed by a real day range | fails open on an empty bar cache |
| Admit prefer band / soft max | `ai_watch_admit_chg_*` (3) | live | delete if the book server ranks by day change | duplicate ranking |
| Arm-ready admission, stale-tape admission | `ai_watch_admit_require_arm_ready`, `ai_watch_admit_max_tape_age_sec`, `_grace_sec`, `_ticks` | live | fold into one freshness rule | three freshness checks at admission |
| Eviction: stale-tape seat cap, unarmable steal, preheat steal, far-%R evict | `_enforce_stale_tape_seat_cap`, `_preferential_*_steal`, `ai_watch_far_exh_evict_sec`, `ai_watch_stale_timeout_*` (5) | live | replace with book server re-rank | four eviction paths; drift source |
| Prefer-square eviction | `ai_watch_admit_prefer_square` | off | delete | |
| Slot priority | `ai_watch_slot_priority` | live | replace with book server rank | |

### Arm (when to buy)

| Item | Keys | Status | Proposal |
|---|---|---|---|
| Fast %R mid-rise cross through −50, slow rising | `ai_watch_exh_mid_rise_arm`, `ai_watch_mid_rise_*` | **live — the one trigger** | keep |
| Square / pre-square / triangle / sticky dual-%R | `ai_watch_exh_square_arm`, `_oversold_triangle_arm`, `_pre_thr`, `_square_max_age_sec`, legacy_arms.py (55 lines) | off | delete |
| MACD gap arm, MACD gates | `ai_watch_macd_*` (5), `ai_watch_arm_require_macd` | off | delete |
| RSI arm gates | `ai_watch_arm_cm_rsi_*` (4), `ai_watch_arm_require_cm_rsi` | off / permissive | delete |
| Below-zone arm | `ai_watch_arm_below_zone(_max_r)` | live | discuss: structure zones vs pure %R |
| Stream-price requirement + 15 s decision age | `ai_watch_arm_require_stream_price`, `ai_watch_decision_max_age_sec=15` | live | **fix** — this is the 43% `tape_only` refusal; see lever 1 |

### Entry checks (keep all; these are the intended safety gates)

$20–$100 price band, live spread cap (`ai_max_spread_pct` 0.25, `ai_max_spread_r` 2.0), session window, max positions (5) and free-cash sizing, daily loss brake (`ai_daily_loss_limit_r` = 1000 for paper — restore 3.0R before live money). Per-name daily entry limit (`ai_watch_max_entries_per_symbol_day` = 0, off): 9/25 re-entries after a loss netted positive, so the limit needs a test, not a default.

### Exits — do not change here

Local trail (arm 0.06R, give 0.35R capped 1% of price), ratchet/capture, T1 scale-out (rr 1.0, 50%), runner trail, `left_overbought`, `dead_trade` (30 min / 0.1R), 90 s minimum hold, 15:50 flatten. MACD liquidate and the limit collar are off (collar saves nothing: [[trading-cost-is-the-spread]]). Overlap to note for an exit session: trail and ratchet both clamp to the peak. 0.25R arm was tested 9/25: net −7.9 bp vs 0.0 (worse).

### Background processes (mini, 9/26)

dashboard.py (Finnhub stream, L2 panel, /api/state), signal_engine (bars → %R), discord_source, trending_screener, movers_screener, ai_trader (book, poll, exits, seed_rank research threads), tools/session_snapshot, tools/watchdog, momentum-monitor, plus an unrelated uvicorn (retirement app). Candidates for the request budget: the dashboard L2 auto-watch.

## Suggested order for the barebones work

1. **Lever 1, data freshness first.** Lower the Alpaca request load (L2 panel cadence, batched quotes) and replace the IEX 15 s last-trade rule with a staleness rule that fits IEX. Measure: `tape_only` share of arm checks, opens per 10 min.
2. **One book.** Book server live; retire soft seed, morning flood, day roster and the four eviction paths.
3. **Delete retired arms**, then legacy_arms.py.
4. **Test the momentum spread exemption off**, on exact replay.

Each step is judged on exact replay (now possible: `replay_session.py --exact`, first real acceptance test on Monday's recording) against the yardstick: concurrency, net bp/trade, cadence, then settings/path count.
