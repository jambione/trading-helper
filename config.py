import json
import os
from pathlib import Path

import desk_core

CONFIG_FILE  = Path(__file__).parent / "config" / "bot_config.json"
SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"
SECRETS_KEYS = ["api_key", "secret_key", "finnhub_key",
                "push_vapid_private_key", "push_contact_email",
                "engine_control_secret"]

DEFAULT_CONFIG = {
    # ── API credentials ──────────────────────────────────────
    "api_key":     os.getenv("ALPACA_API_KEY", ""),
    "secret_key":  os.getenv("ALPACA_SECRET_KEY", ""),
    "finnhub_key": os.getenv("FINNHUB_API_KEY", ""),

    # ── Data fetching ────────────────────────────────────────
    "bar_timeframe":    "1Min",   # 1Min | 5Min | 15Min | 1Hour | 1Day
    "bar_count":        300,
    "scan_interval_sec": 60,

    # ── Signal: %R Trend Exhaustion ─────────────────────────
    "rte_threshold":  20,    # overbought/oversold zone edge (0-50)
    "rte_min_boxes":   2,    # consecutive bars required to be "on deck"

    # ── Signal: CM RSI-2 ────────────────────────────────────
    "cm_rsi_length":    2,   # RSI period (2 = original Larry Connors CM RSI-2)
    "cm_rsi_oversold": 25,   # approaching-oversold threshold for signal

    # ── Signal: OBV Oscillator ──────────────────────────────
    "obv_length": 20,

    # ── Signal: MACD confirmation ───────────────────────────
    "macd_fast":   12,
    "macd_slow":   26,
    "macd_signal":  9,

    # ── Signal: volume surge ─────────────────────────────────
    "volume_surge_mult": 1.5,

    # ── EMA for display ──────────────────────────────────────
    "ema_short": 8,
    "ema_long":  21,

    # ── Williams %R display ──────────────────────────────────
    "wr_length": 14,

    # ── TradingView ──────────────────────────────────────────
    "tv_chart_url":     "https://www.tradingview.com/chart/",
    "brave_tv_tab":     1,    # tab number (1-9) holding TradingView in Brave
    "tv_browser_macos": "Brave Browser",  # browser name for macOS alert automation

    # ── Discord OCR source (the ticker source) ───────────────
    "discord_ocr_poll_sec": 2.5,    # seconds between OCR captures
    "discord_window_owner": "Discord",
    "discord_window_title": "",     # optional window-title substring filter

    # ── Mention burst alerts ─────────────────────────────────
    "mention_alert_threshold": 2,   # mentions within window to trigger alert
    "mention_alert_window":    10,  # rolling window in seconds

    # ── Swing screener (Alpaca bars + Finnhub fundamentals) ──
    # Funnel: Alpaca daily bars (universe + technicals: oversold / reversal /
    # rel-vol / support), then Finnhub stock/metric + recommendation + earnings
    # confirm fundamentals for the technical survivors. Discord confluence is
    # overlaid live by the dashboard.
    "swing_screener_enabled":     False,  # launch swing_screener.py from start_all
    "swing_full_market":          True,   # screen every tradable US equity (~13k) vs a curated list
    "swing_min_dollar_vol":   5000000.0,  # 20-day avg $-volume floor — drops illiquid junk from full market
    "swing_max_price":            100.0,  # hard price ceiling ($)
    "swing_rsi_oversold":         30.0,   # hard gate: RSI(14) must be below this (oversold) —
                                           # also feeds the reversal-signal color tier
    "swing_min_eps_growth":       2.0,    # EPS-growth proxy (peTTM/forwardPE) — SCORING signal, not a hard gate
    "swing_max_eps_growth":       3.0,    # …retained for scoring/config; no longer rejects candidates
    "swing_min_rating":           "Buy",  # min analyst consensus (Buy|Strong Buy)
    "swing_rel_vol_entry":        1.0,    # hard gate: today's vol ÷ 20-day avg must exceed this
    "swing_rel_vol_min":          1.5,    # "high" rel-vol color tier — no longer a hard gate
    "swing_min_rr":               2.0,    # min reward:risk (upside-to-52wk-high ÷ downside)
    "swing_earnings_window_days":  14,    # flag earnings inside this window
    "swing_bar_lookback_days":    150,    # calendar days of daily bars to pull (≈100 sessions)
    # Scheduled runs (ET, HH:MM) — early morning, midday, night. NOT a constant
    # loop: the screener sleeps between these times. A dashboard button also
    # triggers an on-demand refresh.
    "swing_run_times":            ["08:00", "12:30", "20:00"],
    "swing_limit":                12,     # candidates kept in swing_candidates.json
    "swing_enrich_cap":           90,     # max technical survivors sent to Finnhub — calls are now
                                           # paced to the free-tier ~60/min limit, so a full run takes
                                           # a few minutes rather than bursting past the quota

    # ── RS screener (IBD-style relative strength) ────────────
    # rs_screener.py ranks SPLIT-ADJUSTED daily closes against SPY over the FULL
    # bar-covered universe, THEN applies the filters below. Ranking before
    # filtering is the whole point: a percentile taken over the survivors of a
    # strength screen would make "RS 90" mean top 10% of an already-strong
    # subset rather than top 10% of the market.
    "rs_screener_enabled":        False,  # launch rs_screener.py from start_all
    "rs_universe_source":       "alpaca", # alpaca | finviz | file (rs_universe.json always wins)
    "rs_cache_path":  "rs_cache.sqlite",  # SQLite bar cache; rebuild with --rebuild
    "rs_bar_adjustment":        "split",  # split | all — NEVER raw, which is Alpaca's
                                           # default and turns every split into a fake
                                           # ±50-90% move. "all" also restates on every
                                           # ex-div date, so the cache would need daily
                                           # repair across most of the universe.
    "rs_backfill_calendar_days":  400,    # ≈275 sessions; 252 sessions ≈ 366 days + holiday slack
    "rs_lookback_sessions":       252,    # the 12-month anchor
    "rs_overlap_sessions":          5,    # re-fetched every run — this overlap IS the split detector
    "rs_split_tolerance":       0.005,    # close move that means "the vendor restated history";
                                           # smallest common split is 3:2 (+50%), late-print
                                           # revisions are fractions of a cent
    "rs_ffill_limit":               2,    # sessions a price may be carried across a no-print day
    "rs_min_coverage":           0.80,    # fraction of benchmark sessions with a REAL bar
    "rs_min_population":          500,    # below this, no rating is published at all
    "rs_max_stale_frac":         0.10,    # above this, refuse to overwrite the previous file
    "rs_max_p0_staleness_sessions": 1,    # every symbol's P0 must be the same session
    "rs_form":               "trailing",  # trailing (IBD) | quarters (non-overlapping)
    "rs_benchmark":              "SPY",
    "rs_exclude_etp":            True,    # strip ETFs/ETPs from the SERVED list, not the ranking
    "rs_chunk_size":              100,    # symbols per Alpaca request on a backfill
    "rs_incremental_chunk_size":  300,    # …and on the daily path, where only a few
                                           # sessions per symbol come back so the
                                           # 10k-bar page cap is nowhere near binding
    "rs_max_req_per_min":         100,    # of Alpaca's 200 — the key is shared with the engine
    "rs_use_partial_session":   False,    # as_of is always a COMPLETED session
    "rs_settle_after":         "18:00",   # ET; today's bar is excluded before this
    "rs_run_times":          ["18:30"],   # once a day. RS is a 12-month statistic; recomputing
                                           # it midday produces churn carrying no information.
    "rs_limit":                   100,    # rows kept in rs_ratings.json
    # Filters — applied AFTER the percentile.
    "rs_min_rs_rating":            80,
    "rs_min_price":              10.0,
    "rs_min_avg_vol_50d":      500000.0,  # IEX volume, NOT consolidated — bars are IEX-only, so
                                           # this is a fraction of the real tape. See the measured
                                           # error table at tools/morning_funnel.py:196-210.
    "rs_require_above_sma50":    True,
    "rs_require_above_sma200":  False,
    "rs_use_rvol_filter":       False,    # use_X/min_X pairing rather than a null sentinel: the
    "rs_min_rvol":                1.5,    # config panel round-trips an empty number as "", not null
    "rs_use_adr_filter":        False,
    "rs_min_adr_pct":             3.0,

    # ── AI trader (ai_trader.py) — multi-source research desk ─────────────────
    # Shared process/settings (ai_*). Anthropic-specific knobs stay claude_*.
    # Grok-specific knobs stay grok_*. Monitor is display-only.
    #
    # Legacy claude_trader_enabled / claude_trading_enabled / claude_max_price
    # etc. still work via load_config() aliases for one release.
    "ai_trader_enabled":       False,   # launch ai_trader.py from trading/start_all
    # Sole book owner: "grok" | "claude" | "off". Overrides dual legacy flags.
    "ai_trading_source":         "off",
    "ai_trading_enabled":      False,   # legacy Claude book flag (prefer ai_trading_source)
    "ai_max_price":            100.0,
    "ai_quote_poll":            15.0,
    "ai_volume_poll":           60.0,
    "ai_avg_days":                10,
    "ai_rvol_time_adjusted":    True,
    "ai_trade_amount":        1000.0,
    # Slightly higher so trending heat can share slots with momentum (3 left
    # only 4 pure-trending fills on 2026-08-11 under slot contention).
    "ai_max_positions":            4,
    "ai_max_buys_per_poll":        2,
    "ai_max_sells_per_poll":       5,
    "ai_risk_pct":               1.0,
    "ai_trade_style": "Day scalp",
    # Must be <= ai_watch_synth_rr or every synthetic zone self-blocks on the
    # reward_risk gate in should_arm_buy. Day scalp uses sub-1R first targets.
    "ai_min_reward_risk":        0.5,
    "ai_positions_poll_sec":     5.0,
    "ai_prompt_file": "ai_prompt.txt",
    # Safety / desk quality knobs
    # Protective stop shape. Stop-MARKET guarantees the exit fills; the limit
    # form can miss entirely on a gap and leave the position naked long.
    # Concentration cap. Risk sizing alone says nothing about position size:
    # a tight stop implies a huge notional, and nothing checked buying power.
    "ai_max_position_pct":        25.0,     # max % of equity in one name
    "ai_reentry_cooldown_sec":    900.0,    # no re-arm this soon after an exit
    # After a wash-trade broker reject, freeze the symbol this long so the
    # poller does not re-place every 20s (2026-08-11 QMCO thrash).
    "ai_wash_cooldown_sec":      1800.0,
    # ── Entry order shape ───────────────────────────────────────────────────
    # "limit" (default) or "market". A market entry fills at whatever the ask
    # is at execution, which breaks the premise of the zone: size_by_risk sizes
    # off current_ask and the stop is derived from it, so a fill above the
    # quote makes real risk exceed ai_watch_synth_stop_pct and notional exceed
    # ai_max_position_pct. These are thin IEX books on high-RVOL names, where
    # that slippage is largest.
    "ai_entry_order_style":    "limit",
    # Marketable pad above the ask, then hard-capped at the zone top — so a
    # fill can never land outside the entry zone.
    "ai_entry_limit_pad_pct":     0.15,
    # An unfilled entry limit is cancelled after this long: if price left the
    # zone the setup is gone, and re-evaluating beats leaving a stale order
    # resting while the zone re-anchors away from it. Distinct from
    # ai_entry_unconfirmed_ttl_sec, which covers a *filled* but unconfirmed fill.
    "ai_entry_limit_ttl_sec":     30.0,
    # True → stop-MARKET (default: gap through the trigger still fills).
    # False → stop-LIMIT with ai_stop_limit_slip_pct room; can miss entirely
    # on the high-RVOL names this book selects for.
    "ai_stop_use_market":         True,
    "ai_stop_limit_slip_pct":      1.0,     # room under trigger when stop-LIMIT
    "ai_entry_unconfirmed_ttl_sec": 900.0,  # cancel unfilled managed entries
    "ai_daily_loss_limit_r":        3.0,    # stop new entries after -NR today
    # PDT on the AI entry path. "block" refuses a new buy at 3 same-day
    # round-trips in 5 business days when equity is under $25k. Broker
    # daytrade_count is preferred; TradeGuard is the restart-proof fallback.
    # Does not replace ai_daily_loss_limit_r (R) with a dollar kill switch.
    "ai_pdt_protect":            "block",   # block | warn | off
    "ai_max_open_risk_pct":         5.0,    # sum open stop-risk % equity
    "ai_open_bell_enabled":        True,    # act on overnight ideas after open
    "ai_open_bell_time":        "09:35",    # ET
    # Cancel all open orders + close all positions once per day at this ET time.
    "ai_eod_liquidate_enabled":    True,
    "ai_eod_liquidate_time":    "15:50",    # ET — flatten before the 16:00 close
    # At RTH open: flatten everything once before any new paper entries.
    "ai_sod_liquidate_enabled":    True,
    # Daily A vs X duel: dual trial → R score → winner-only chance 3.
    "ai_duel_enabled":             False,
    # Force-flat dual legs this many minutes before the next research slot.
    "ai_duel_close_before_research_min": 10,
    # Optional override for final dual cut; empty → derive from research times − lead.
    "ai_duel_trial_end_time":       "",
    "ai_duel_chance3_time":     "14:30",   # usually = last research slot
    "ai_require_agreement":       False,    # only trade AX-agreed names
    # Reject entry if bid/ask wider (% of mid). Paid twice — in and out — so it
    # is measured against the first target, not the stop: at the old 1.0% an
    # accepted trade could pay its entire 0.6R T1 to the spread.
    "ai_max_spread_pct":           0.25,
    # The same cost in R: reject when a round trip (crossed twice) exceeds this
    # fraction of the distance to the stop. 0 = off, which is the shipped
    # default — the live path reads IEX quotes, which are a few percent of the
    # tape and always look wide, so this would block good fills. Turn it on
    # once outcomes.jsonl has real entry_slippage_r to calibrate against.
    "ai_max_spread_r":              0.0,
    "ai_min_dollar_volume":         0.0,    # 0 = off; else require row dollar_volume
    # Entry watch poller (agreement queue + structure TTL / arming)
    "ai_watch_enabled":                 True,   # enable entry-watch queue
    # Weekdays: seed/sync AI Watch from this ET time until EOD liquidate.
    "ai_watch_start_time":           "09:00",   # ET — watching begins
    "ai_watch_require_agreement":      False,   # only watch AX-agreed names
    "ai_watch_single_source":          False,   # allow single-source watch when True
    "ai_watch_poll_sec":                20.0,   # watch poll interval (seconds)
    "ai_structure_ttl_sec":           5400.0,   # structure plan TTL (seconds)
    "ai_watch_expire_at_close":         True,   # drop watches at session close
    "ai_entry_zone_pad_pct":             0.0,   # pad around entry zone (%); 0 = exact zone
    "ai_max_structure_calls_per_hour":    12,   # rate-limit structure LLM calls
    "ai_persist_entry_decisions":       True,   # persist entry decision records
    # Counterfactual log: one row per watched symbol per poll, off the price
    # the poller already fetched (never an extra API call). outcomes.jsonl
    # only grows on a fill and the desk can run a whole session without one,
    # which leaves every gate unmeasurable — see tools/shadow_report.py.
    # No protective exit, no position. Every bare buy path in alpaca_trader
    # (bare limit, bare market, extended-hours limit, and buy() when brackets
    # are unconfigured) refuses while this is on. Risk-sized entries are
    # unaffected — they go through buy_limit_bracket / buy_bracket_exact.
    # Turning it off re-enables the path that opened CELH unhedged at 83% of
    # equity on 2026-08-06.
    "require_protective_exit":          True,
    # Only this host may place/cancel/replace/liquidate. Empty = unrestricted
    # (single-box default). Both machines shared one Alpaca paper key: risk
    # caps are per-instance so real exposure could double, liquidate_all
    # flattens the WHOLE account so whichever box hit 15:50 first closed the
    # other's book, and 3 of 4 outcome records were the other machine's
    # trades. Reads are never gated — a dev box may observe, not act.
    "ai_trading_host":                    "",
    "ai_shadow_log_enabled":            True,
    # The other arm of the selection question: sample what admission turned
    # away, so a gate can be judged on what it removed and not only on what it
    # passed. Throttled per symbol; prices come off rows the screeners already
    # refreshed, never a fresh quote.
    "ai_reject_log_enabled":            True,
    # Hard cap on the momentum watchlist (dashboard.TICKER_MAX_COUNT). The
    # 15-minute age purge did not bound it — feeds re-add faster than it
    # retires — so it drifted to 26+, each entry costing a quote on a desk
    # already past Alpaca's rate limit. Read once at dashboard import.
    "momentum_max_tickers":                8,
    # Minimum time-adjusted relative volume for a momentum *candidate* to stay
    # on the dashboard watchlist (dashboard.TICKER_MIN_RVOL). Once rvol is
    # known, below this → refuse add + purge on load. Unknown rvol is allowed
    # until the first volume sample. Held positions and src=book rows are
    # exempt. 0 disables. Read once at dashboard import.
    "momentum_min_rvol":                  2.0,
    # Ceiling on symbols carrying real-time data at once — momentum candidates
    # plus every name the AI Watch book is on. Bounded by Finnhub's free tier
    # (~50 concurrent WS subscriptions desk-wide, unenforced by the client), so
    # this stays under it with headroom. Unlike momentum_max_tickers this is not
    # a display preference: over the ceiling, symbols silently stop receiving
    # trades. Read once at dashboard import.
    "realtime_symbol_budget":             40,
    # Seed entry-watch from live desk heat (structure poller still defines levels).
    "ai_watch_seed_momentum":           True,
    "ai_watch_seed_momentum_n":           12,
    # Momentum Stocks panel → AI Watch with a soft path (no score/indicators).
    # RVOL + uptrend still apply when known. Prefers Stocktwits overlap.
    "ai_watch_seed_momentum_open":      True,
    "ai_watch_seed_momentum_open_n":      10,
    "ai_watch_seed_trending":           True,
    "ai_watch_seed_trending_n":           20,
    # Trending shortlist floors (looser than momentum's 50% day-move bar).
    # Seed needs score > min OR day chg ≥ this OR rvol ≥ trending min rvol.
    "ai_watch_trending_min_pct_change": 15.0,
    "ai_watch_trending_min_rvol":        1.5,
    # AI Research boards (grok_suggestions.json / claude_suggestions.json) as a
    # third seed. research_candidate_rows() has always existed; nothing called
    # it, so the boards fed the watch book only through the side door that keeps
    # submitted/filled names and duel champions across a sync.
    "ai_watch_seed_research":           True,
    "ai_watch_seed_research_n":           12,
    # "Bullish Bob LIVE" call-outs (dashboard /api/state -> bb_live.history) as a
    # fourth seed. A call-out carries a symbol, free text and a timestamp and
    # nothing else — no score, no rvol, no price — so freshness is the only
    # signal it can offer on its own. Everything numeric comes off the desk row
    # for the same symbol, and passes_inclusion still has to clear it.
    "ai_watch_seed_bb_live":            True,
    "ai_watch_seed_bb_live_n":             6,
    # How long a call-out stays a candidate. Deliberately longer than the
    # header chip's own freshness window (_BB_LIVE_FRESH_SEC, 5 min): the chip
    # answers "what is he on right now", this answers "is this still worth
    # watching", and a called name needs time to reach its entry.
    "ai_watch_bb_live_fresh_sec":      900.0,
    # Restrictive AI Watch filters
    # Trending: score > min OR day chg / rvol claim; WASH never seeded.
    # EXT is optional unless ai_watch_require_look_ext is true.
    "ai_watch_trending_min_score":      5.0,  # Stocktwits score must be > this
    "ai_watch_min_pct_change":         50.0,  # day chg % for momentum big-mover seed
    # Same bar as momentum_min_rvol (dashboard watchlist). Known rvol below
    # this refuses AI Watch admission; unknown abstains (provisional).
    # Trending seed uses ai_watch_trending_min_rvol (default 1.5) instead.
    "ai_watch_min_rvol":                2.0,
    # Cap how many LOOK tags apply_look_highlights may set panel-wide.
    "ai_watch_look_max":                  20,
    # False (default after 2026-08-11): allow non-EXT trending heat onto the
    # book; WASH still blocked. True: legacy EXT-only trending path.
    "ai_watch_require_look_ext":       False,
    # ── Strict inclusion (conjunctive — every enabled gate must pass) ────────
    # The old rules OR'd four criteria and admitted on any one. Three could
    # never fire (rvol was None on every trending row, nothing hit the 50% bar,
    # momentum contributed nothing), so the book was selected by Stocktwits
    # popularity alone and 4 of 6 admitted names were *down* on a long-only desk.
    "ai_watch_require_uptrend":        True,  # day change must be positive
    # ADMISSION does NOT require indicators. benchmarks/ab_bench_* (2026-06-12)
    # found the indicator entry has no standalone edge on this pool — every
    # config lost money. The catalyst (mention burst / trending heat / big
    # move) is the edge; the indicators only TIME it. Filtering admission on
    # timing throws away the edge and re-anchors the zone on every re-admit.
    "ai_watch_require_indicators":    False,  # admission: catalyst is the edge
    "ai_watch_min_proximity":            67,  # >=67 = "aligning"; 100 = "buy_zone"
    # Escalating bar: 67 to sit on the book, 100 (all three indicators) to
    # actually arm. Re-checked at arm time, not just at admission — a name can
    # fade between joining the book and price reaching the zone.
    # ARMING does require them — that is where timing is the actual question.
    # When False, in-zone price alone can arm (indicators still used if present
    # for sell_signal / optional arm_min_proximity). Operator desk often has no
    # engine indicator map for book symbols; requiring the triple blocked every buy.
    "ai_watch_arm_require_indicators": False,
    # Named conditions, not a count. proximity_pct only says "how many of the
    # three hold", so a count of 100 silently demanded MACD too — and MACD is
    # the laggard (see strategy_three_indicator.buy_signal). CM RSI-2 and %R
    # exhaustion are the actual buy signals.
    "ai_watch_arm_require": ["cm_ok", "pctr_ok", "cm_rsi_rising"],
    "ai_watch_arm_min_proximity":         0,  # 0 = off; named flags are the test
    # sell_signal gated four ENTRY checks and nothing on the way out: the desk
    # refused to buy a flagged name, then held that same name silently when the
    # flag turned on later. This tightens the stop to entry rather than
    # closing — there is no completed-trade history yet to say the signal beats
    # letting the bracket work, and it only acts when price is ABOVE entry
    # (below it, a breakeven stop sits above the market and fills instantly,
    # which is a market exit, not a tighter stop).
    "ai_sell_signal_breakeven":        True,
    # Exit-side shadow log — one row per tick per OPEN position, including the
    # ticks where nothing happened. The buy side samples every candidate and
    # records why it did NOT arm, which is what makes its refusals priceable;
    # the moment a position opened it left the telemetry entirely (4099 shadow
    # rows on 2026-08-07, every one status=watching), so "should we have sold
    # there?" had no data behind it at all. Writes position_shadow.jsonl.
    "ai_position_shadow_enabled":      True,
    "ai_watch_min_adx":                 0.0,  # 0 = off until the engine publishes ADX
    "ai_watch_min_price":               1.0,  # no sub-$1 names
    "ai_watch_admit_ticks":               1,  # consecutive qualifying polls to admit
    # ── Real-time tape pre-filter ───────────────────────────────────────────
    # The Finnhub WebSocket price (via the dashboard's ticker rows) is used to
    # SKIP the per-symbol Alpaca quote when price is nowhere near the zone.
    # It is never used to arm: the socket carries trades, not quotes, and a
    # print at the bid would arm on a price the order cannot actually get.
    # Max symbols we push into the signal engine for indicator computation.
    # Finnhub's free tier allows ~50 concurrent WS subscriptions desk-wide and
    # nothing enforces it, so leave headroom for the engine's own tickers.
    "ai_watch_engine_push_max":          24,
    "ai_watch_stream_enabled":         True,
    "ai_watch_stream_max_age_sec":     10.0,  # older than this → fall back to REST
    "ai_watch_stream_skip_margin_pct":  1.0,  # only skip when this far outside
    # Synthetic pullback zone when model has no levels (Mom/ST).
    "ai_watch_synth_zone_enabled":     True,
    # Zone construction mode for synthetic levels:
    #   "double_bottom" — two matching swing lows on 1m bars; buy band from
    #                     support up ~1.25% (tiny pad under). Falls back to
    #                     "offset" when bars/pattern unavailable.
    #   "offset"        — % under the live print (legacy).
    "ai_watch_zone_mode":     "double_bottom",
    # In double_bottom mode, refuse to arm on the offset fallback. Without this
    # a name with no detectable shelf quietly becomes a different trade — a
    # percentage band with a 5% stop, the regime the 90-day replay measured at
    # -0.0027R — and lands in outcomes.jsonl indistinguishable from a real
    # double-bottom entry. Set False to allow the fallback to trade again.
    # Zone geometries allowed to arm a real order. "offset" is deliberately
    # absent: a fixed percentage band measured -0.0027R over a 1,220 symbol-day
    # replay. "pullback_band" is sized from each symbol's own dip distribution
    # and is scored separately via the zone_kind stamped on every outcome row.
    "ai_watch_armable_zone_kinds": ["double_bottom", "pullback_band"],
    # A pullback that overshoots the zone is still a pullback. Bounded in the
    # trade's own risk unit (R = zone floor - stop), so the allowance scales
    # with the setup instead of meaning several R on a tight structural stop
    # and a fraction of one on a wide synthetic stop.
    # Edge mode (2026-08-11 postmortem → Option A default).
    #   continuation     — arm on heating|overbought in zone; hold through OB;
    #                      exit via stop / broker T1 / trail / dead_trade.
    #                      left_overbought is OFF (it was the small-loss factory).
    #   exhaustion_scalp — arm overbought-only; sell when %R leaves the band.
    "ai_edge_mode":              "continuation",
    # Explicit override for left_overbought software exit. None/absent → follow
    # edge mode (on only for exhaustion_scalp). Set false to force off.
    # "ai_exit_left_overbought": False,
    # Exhaustion / %R rules for arm geometry (still used under continuation).
    # BUY (continuation): heating above heat_min OR overbought.
    # BUY (exhaustion_scalp): overbought only.
    # A missing %R reading refuses the buy when require_data is true.
    "ai_watch_exhaustion_rules":      True,
    # Recompute %R against the live price instead of trusting the engine's
    # 60-120s-old copy. Closed bars give the window, the live quote gives the
    # close — no new market data, just no waiting for a bar to close.
    "ai_watch_exhaustion_live":       True,
    # Fade must persist this long before selling. SECONDS, not polls: the
    # position loop runs every 5s against a 60s engine refresh, so a poll count
    # measured the same stale reading repeatedly and fired ~12x early.
    # Legacy sustained-fade exit. Superseded by the band crossing above, which
    # is a level test and therefore immediate; kept only for the fallback path.
    "ai_watch_exhaustion_exit_sec":  120.0,
    # Give-back below the band before the exit fires, in exhaustion points.
    # 0 = sell the instant it leaves overbought (operator's stated rule).
    "ai_watch_exhaustion_exit_give_pct": 0.0,
    # Under continuation: minimum exhaustion % for a *heating* arm (0–100).
    # Overbought band still arms regardless. Under exhaustion_scalp: unused.
    "ai_watch_exhaustion_heat_min_pct": 50.0,
    # SELL side only. A held name with no %R reading keeps the pre-exhaustion
    # sell_signal stop defence — taking its only indicator defence away while
    # giving it no replacement would leave it worse off than before. Never flip
    # this to gate entries; that is what require_data below is for.
    "ai_watch_exhaustion_fallback":   True,
    # BUY side. Refuse to arm a name that has no %R reading at all, instead of
    # letting it through on the pre-exhaustion gates.
    #
    # "No reading" is a coverage test, not a preference: live_exhaustion needs
    # rte_fast_length + 2 bars (23) to form the window, and on the free IEX feed
    # a thin name simply does not print that many — AKAN managed 3 bars in ten
    # days. A name IEX prints once a day is one we cannot see, and the fill
    # would be poor even if the indicator liked it.
    #
    # The cost is small. Measured 2026-08-11 against all 96 names the desk
    # actually watched on 08-10: 4 blind (HWH, VSA, LXEH, LIVE), 92 clear the
    # window, most at the full 90 bars. Earlier estimates of ~1 in 5 were
    # inflated by the ascending-sort bar bug, which served week-old bars and
    # made coverage look far worse than it is.
    #
    # Watch the BLIND line in desk_report section 5. If it climbs toward the
    # whole book the constraint is the data feed (SIP), not this gate.
    "ai_watch_require_exhaustion_data": True,
    # Widest the %R window may stretch, as a multiple of its nominal duration
    # (21 bars x 60s = 21 min, so 3.0 allows ~63 min). 0 disables the check.
    #
    # The bar-count gate above catches names with no data. This catches the
    # more dangerous case: enough bars, spread over far too long. IEX prints a
    # thin name a few times an hour, so 23 bars can span days while %R happily
    # returns a clean number for what it thinks is a 21-minute window.
    # Measured 2026-08-11 over the 96-name book, of the 92 that pass the bar
    # count: 62% span under 40 min, 17% 40min-2h, 12% 2h-1d, and 9% span MORE
    # THAN A DAY — NEGG's "21-minute" window covered 7,120 minutes.
    #
    # 3.0 is a judgment call, not a measured optimum, same status as
    # heat_min_pct. It refuses roughly a quarter of the book. Sweep it against
    # the window_span column in shadow before trusting the number.
    "ai_watch_exhaustion_max_window_mult": 3.0,
    # Nominal seconds per bar for the span check above. Tracks
    # ai_watch_db_bar_timeframe, which is 1Min.
    "ai_watch_db_bar_seconds":        60.0,
    "rte_fast_ewm_span":                 7,   # matches signals.py smoothing
    "rte_direction_eps":              0.05,   # %R move below this is not a turn
    # Below the band still arms (operator: buy in *or below* the zone). Cap is
    # in R units of (zone floor − stop). 1.0 = all the way to the stop; min_stop
    # still refuses fills with no room left. 0.5 was too tight — dips through
    # the floor looked "below zone" and never traded.
    "ai_watch_arm_below_zone":        True,
    "ai_watch_arm_below_zone_max_r":   1.0,
    "ai_watch_require_db_zone":       True,
    # Minimum risk per share, as % of the price paid, before an entry may arm.
    # The double-bottom band spans S*0.9975 to S*1.0125 against a stop fixed at
    # S*0.995, so risk per share is 0.25% of price at the bottom of the band and
    # 1.73% at the top — a 6.9x swing set purely by where the fill lands. At the
    # tight end, sizing asks for ~400% of equity and the notional cap silently
    # becomes the position sizer. Decline the fill rather than move a stop that
    # is deliberately structural.
    "ai_watch_min_stop_pct":          0.5,
    # Double-bottom geometry (see find_double_bottom_support / build_db_zone).
    "ai_watch_db_above_pct":           1.25,  # entry_high = S * (1 + above/100)
    "ai_watch_db_below_pct":           0.25,  # entry_low  = S * (1 - below/100) fill pad
    "ai_watch_db_stop_below_pct":      0.50,  # stop under the lower of the two lows
    "ai_watch_db_match_pct":           0.40,  # two lows "same support" if within this %
    "ai_watch_db_swing_bars":             2,  # pivot: lower than this many bars each side
    "ai_watch_db_min_sep_bars":           3,  # min bars between the two bottoms
    "ai_watch_db_lookback_bars":         90,  # recent 1m bars to scan
    "ai_watch_db_bar_refresh_sec":     120.0,  # throttle REST bar pulls per symbol
    "ai_watch_db_require_price_above": True,  # only arm structure if last > support
    # 2.0 (not 5.0): at a 5% offset the zone is built 5% under the print and
    # re-anchors up on every tick, so the ask sits a permanent +5.26% above the
    # zone top (1/0.95-1) and only a 5% break from the high-water mark ever
    # fills. above_zone was 51% of all skips. 2% fills on a routine pullback.
    # Variable pullback band. Depth is measured from the name's OWN completed
    # pullbacks (ai_entry_watch.pullback_depths) rather than set as one global
    # percentage, because the right depth for RIG (deepest dip 1.0% on
    # 2026-08-10) and for BTDR (20.6% the same day) are not the same number.
    # Percentiles, not multiples of ATR: an ATR multiple means a different
    # depth on 1Min than on 5Min bars, so retuning the bar timeframe would
    # silently move every zone.
    "ai_watch_zone_variable":         True,
    "ai_watch_zone_top_pctl":         65.0,   # zone ceiling: a dip it makes often
    "ai_watch_zone_bottom_pctl":      90.0,   # zone floor: deep but still routine
    "ai_watch_zone_dip_window_bars":    15,   # rolling window the dips are measured over
    "ai_watch_zone_top_min_pct":       0.8,   # never buy within this of the print
    "ai_watch_zone_top_max_pct":       3.0,
    "ai_watch_zone_bottom_min_pct":    1.2,
    "ai_watch_zone_bottom_max_pct":    9.0,   # past this it will not come back today
    "ai_watch_zone_min_width_pct":     0.6,
    "ai_watch_zone_min_samples":        10,   # fewer dips than this → not measured
    # Depth available shrinks with the session: a 4% dip is an ordinary 10:00
    # event and a fantasy at 15:30.
    "ai_watch_zone_time_decay":       True,
    "ai_watch_zone_decay_floor":       0.5,
    "ai_watch_zone_offset_pct":        2.0,  # entry_high = last * (1 - offset/100)
    # 4.0, not 2.0: the zone re-anchors to `offset` below every new high, so
    # price must fall that far from the RUNNING high-water mark and any upward
    # tick resets it. At 2/2 that put price in the zone on 24 bars across 50
    # symbol-days (~0.04 arms/sym/day). Widening the band keeps the entry
    # STARTING at -2% and just keeps buying deeper: 271 in-zone bars, 27 arms.
    "ai_watch_zone_width_pct":         4.0,  # zone depth below entry_high
    # Measured off the *fill*, not entry_low — see _decision_for_place.
    "ai_watch_synth_stop_pct":         5.0,  # stop under the fill price
    # Day Scalp v0: first bank inside a normal day's range. 1.5R at 5% stop
    # demanded +7.5% (above p75 of day range) and ~77% of replay exits hit the
    # 15:50 clock instead. 0.6R ≈ +3% — reachable for small consistent banks.
    "ai_watch_synth_rr":               0.6,  # target at this R multiple
    # Scale-out / runner (synthetic dual tranche when ai_day_scalp_dual_tranche).
    "ai_watch_synth_scale_out_pct":   50.0,  # % of shares with T1 take-profit
    # Runner trail after T1, in R — a percent trail is a different trade on
    # every name (2.5% is 2.5R behind a 1% stop, 0.5R behind a 5% one), which
    # let the runner lose more than tranche A had just banked. The stop is
    # floored at breakeven, so a trade that reaches T1 cannot finish red.
    "ai_runner_trail_r":               1.0,
    "ai_runner_step_r":                0.1,  # min ratchet gain before re-placing
    # Display/telemetry only since the runner moved to R. Kept so the zone
    # payload and UI keep their field.
    "ai_watch_synth_trail_pct":        2.5,
    # Dual bookkeeping (Option A day scalp): ONE parent buy for full size with
    # a hard stop, then after fill attach a partial T1 limit for scale_out_pct
    # and trail the remainder. Never a second buy — two protected buys on the
    # same symbol were Alpaca wash-trade rejects (40310000) and left naked
    # longs after rollback (AXTI 2026-08-10).
    "ai_day_scalp_dual_tranche":      True,
    # Dead trade: still flat/red with tiny MFE after N minutes → market out.
    # Dead trade: no scale-out, MFE never reached, still flat/red → flatten.
    # Continuation wants this tighter so positions do not sit 90m for −0.3R
    # (S/CRCL on 2026-08-11) waiting for an exhaustion exit that is now off.
    "ai_dead_trade_min":               20.0,
    "ai_dead_trade_mfe_r":            0.25,
    # Exit-side decision log while held (MAE/MFE, exit_why). tools/exit_report.
    "ai_position_shadow_enabled":     True,
    # On sell_signal while green, move stop to entry (never loosen).
    "ai_sell_signal_breakeven":       True,
    # ── Capital first, profit second ────────────────────────────────────────
    # Every live long must have a path to lose only the planned R (broker stop)
    # and a path to bank the thesis (software exhaustion). A take-profit limit
    # alone is not protection. An orphan fill with no managed row is a capital
    # leak (MLTX 2026-08-11): no stop heal, no left_overbought.
    #
    # If an open long has no resting sell STOP, place one from managed state.
    # Take-profit limits do not count as protection (prefer stop over target).
    "ai_heal_unprotected":            True,
    # Re-home broker-live symbols missing from positions_state when we can
    # recover stop/entry from entry_ok (or a resting stop). Enables heal +
    # exhaustion on lost-update orphans.
    "ai_adopt_unmanaged":             True,
    # Unmanaged + unprotected + no recoverable stop → flatten. Missing the
    # trade beats a naked long. Human positions with a resting stop are left
    # alone (not adopted without entry_ok; not flattened if stop rests).
    "ai_flatten_unmanaged_unprotected": True,
    # Rest a broker take-profit. With dual tranche this is a *partial* T1
    # (scale_out_pct) attached after the parent fill so it does not wash the
    # resting buy. Without dual, full-size OTOCO TP on the parent when possible.
    "ai_entry_broker_target":          True,
    # Re-anchor frozen synth zone when last is this far above entry_high (%).
    # 0.0 = track the real-time price every poll (no deadband).
    "ai_watch_synth_reanchor_pct":     0.0,

    # Anthropic (Claude) research source — provider-specific
    "claude_research_enabled":   False,
    "claude_backend":       "claude_cli",
    "claude_cli_bin":          "claude",
    "claude_model":            "sonnet",
    "claude_effort":            "xhigh",  # low|medium|high|xhigh|max
    "claude_research_times": ["08:30", "11:30", "14:30"],
    "claude_research_weekdays_only": True,
    "claude_research_catchup_min": 120,
    "claude_request_timeout":   600.0,
    "claude_live_search":        True,
    # web_x = web_search + x_search on xAI API; Claude CLI uses WebSearch/WebFetch.
    "claude_search_tools":      "web_x",
    "claude_max_turns":             8,
    "claude_max_output_tokens": 10000,
    "claude_use_prior_context":  True,
    # RS leaders + Stocktwits heat + peer AI board (compact research inject).
    "claude_use_desk_snapshot":  True,
    "claude_save_reports":       True,
    # Legacy aliases (mirrored in load_config from ai_* when missing)

    # ── Grok research source (xAI subscription via Grok CLI) ──────────────────
    "grok_research_enabled":   False,   # scheduled Grok research via ai_trader.py
    "grok_trading_enabled":    False,   # preferred sole paper-trading owner
    "grok_max_price":          100.0,   # display / idea price ceiling ($)
    "grok_backend":            "cli",   # subscription: grok CLI / grok login
    "grok_cli_bin":            "grok",
    "grok_model":              "grok-4.5",
    "grok_max_turns":               4,  # A/B: t4 beat t8 on quality/token impact
    "grok_live_search":         True,
    # Same modes as claude_search_tools; used when grok_backend=api.
    "grok_search_tools":       "web_x",
    "grok_use_prior_context":  False,
    "grok_use_desk_snapshot":  True,
    "grok_research_times": ["08:30", "11:30", "14:30"],
    "grok_research_weekdays_only": True,
    "grok_research_catchup_min": 120,
    "grok_prompt_file": "ai_prompt.txt",  # shared research prompt for now
    "grok_request_timeout":   600.0,
    "grok_save_reports":       True,

    # ── Trending screener (trending_screener.py) ──────────────────────────────
    # Stocktwits carries no usable quotes, so rows are enriched from Alpaca.
    # LOOK badges are NOT computed here — those thresholds are desk display
    # settings and stay in the monitor's momentum_config.json. max_price is
    # applied by the dashboard (web panel) and the monitor (display_rows).
    "trending_screener_enabled": False,   # launch trending_screener.py
    "stocktwits_poll":           60.0,    # seconds between Stocktwits polls
    "stocktwits_quote_poll":     15.0,
    "stocktwits_volume_poll":    60.0,
    "stocktwits_stocks_only":    True,
    "stocktwits_avg_days":         10,
    "stocktwits_rvol_time_adjusted": True,
    "stocktwits_max_price":      35.0,    # hide names at/above this last ($)
    "trending_max_price":        35.0,    # alias used by older bot_config keys
}

def validate_ai_config(cfg: dict) -> list[str]:
    """Return human-readable problems with the AI desk's settings.

    Every rule here corresponds to a combination that has actually shipped and
    silently misbehaved — a knob that reads fine on its own but contradicts
    another one. Callers surface these as operator warnings rather than
    raising, so a bad edit degrades to a visible complaint, not a dead desk.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    out: list[str] = []

    def _f(key, default=0.0):
        try:
            return float(cfg.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return float(default)

    min_rr = _f("ai_min_reward_risk", 1.5)
    synth_rr = _f("ai_watch_synth_rr", 1.5)
    if synth_rr > 0 and min_rr > synth_rr:
        out.append(
            f"ai_min_reward_risk ({min_rr:g}) > ai_watch_synth_rr ({synth_rr:g}) — "
            "every synthetic zone will self-block on the reward_risk gate"
        )

    offset = _f("ai_watch_zone_offset_pct", 2.0)
    reanchor = _f("ai_watch_synth_reanchor_pct", 0.0)
    if offset > 0 and reanchor >= offset:
        out.append(
            f"ai_watch_synth_reanchor_pct ({reanchor:g}) >= "
            f"ai_watch_zone_offset_pct ({offset:g}) — the zone can never re-anchor"
        )

    stop_pct = _f("ai_watch_synth_stop_pct", 5.0)
    risk_pct = _f("ai_risk_pct", 1.0)
    max_pos = int(cfg.get("ai_max_positions", 5) or 0)
    if stop_pct > 0 and risk_pct > 0 and max_pos > 0:
        per_pos = risk_pct / (stop_pct / 100.0)
        total = per_pos * max_pos
        if total > 100.0:
            out.append(
                f"ai_risk_pct {risk_pct:g}% with a {stop_pct:g}% stop implies "
                f"{per_pos:.0f}% of equity per position; {max_pos} positions = "
                f"{total:.0f}% — over 100% needs margin and orders may be rejected"
            )
        cap = _f("ai_max_position_pct", 0.0)
        if cap > 0 and per_pos > cap:
            out.append(
                f"ai_max_position_pct ({cap:g}%) will clamp every entry — risk "
                f"sizing alone implies {per_pos:.0f}% per position"
            )

    # In double_bottom mode the stop is structural and typically well under 2%,
    # so the notional cap binds on EVERY entry and ai_risk_pct stops being the
    # thing that sets risk. Say so rather than letting the R unit quietly mean a
    # different number of dollars on every trade.
    cap = _f("ai_max_position_pct", 0.0)
    if (
        str(cfg.get("ai_watch_zone_mode") or "").lower() in
        ("double_bottom", "db", "structure")
        and cap > 0 and risk_pct > 0
    ):
        binds_below = 100.0 * risk_pct / cap
        min_stop = _f("ai_watch_min_stop_pct", 0.0)
        if min_stop < binds_below:
            out.append(
                f"in double_bottom mode the notional cap ({cap:g}%) binds on any "
                f"stop tighter than {binds_below:.1f}% — with ai_watch_min_stop_pct "
                f"at {min_stop:g}%, ai_risk_pct ({risk_pct:g}%) does not set the "
                f"size, the cap does"
            )

    # Real-time coverage budget. The book pushes candidates into the dashboard's
    # ticker list so they carry live tape (ai_entry_watch.push_candidates_to_engine
    # → stream_quote); anything that does not fit falls back to a REST ask+bid
    # per symbol per poll. On 2026-08-10 the push cap was 24 against a 10-slot
    # list, which thrashed: 305 evictions in 40 minutes and 882 rate-limit
    # errors. The two numbers have to be sized against each other or the
    # overflow is silent.
    push_max = int(cfg.get("ai_watch_engine_push_max", 24) or 0)
    mom_max = int(cfg.get("momentum_max_tickers", 8) or 0)
    budget = int(cfg.get("realtime_symbol_budget", 40) or 0)
    if budget > 0 and push_max > 0 and mom_max + push_max > budget:
        out.append(
            f"realtime_symbol_budget ({budget}) is under momentum_max_tickers "
            f"({mom_max}) + ai_watch_engine_push_max ({push_max}) = "
            f"{mom_max + push_max} — the overflow gets no live tape and falls "
            f"back to per-symbol REST quotes"
        )

    spread = _f("ai_max_spread_pct", 1.0)
    if stop_pct > 0 and spread > 0 and stop_pct <= spread:
        out.append(
            f"ai_watch_synth_stop_pct ({stop_pct:g}%) <= ai_max_spread_pct "
            f"({spread:g}%) — the stop sits inside the quoted spread"
        )
    # The spread is paid twice — crossing in and crossing out — so it must be
    # small against the FIRST target, not just against the stop. At the old
    # 1.0% cap against a 0.6R target on a 1.6% double-bottom stop, an accepted
    # trade could hand its entire T1 to the market maker.
    t1_pct = synth_rr * stop_pct
    if spread > 0 and t1_pct > 0 and 2.0 * spread > 0.5 * t1_pct:
        out.append(
            f"ai_max_spread_pct ({spread:g}%) is wide against the first target "
            f"({synth_rr:g}R = {t1_pct:.2f}% on a {stop_pct:g}% stop) — "
            f"round-trip spread can consume {200.0 * spread / t1_pct:.0f}% of T1"
        )

    def _hhmm(key, default):
        raw = str(cfg.get(key) or default).strip()
        try:
            h, m = raw.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    start = _hhmm("ai_watch_start_time", "09:00")
    bell = _hhmm("ai_open_bell_time", "09:35")
    eod = _hhmm("ai_eod_liquidate_time", "15:50")
    if None not in (start, bell, eod) and not (start <= bell < eod):
        out.append(
            "session clock out of order: expected ai_watch_start_time <= "
            "ai_open_bell_time < ai_eod_liquidate_time"
        )

    prox = _f("ai_watch_min_proximity", 67)
    if cfg.get("ai_watch_require_indicators", True) and prox > 100:
        out.append(
            f"ai_watch_min_proximity ({prox:g}) exceeds 100 — nothing can ever "
            "be admitted to the watch book"
        )

    # Option A / exit coherence (BIVI 2026-08-12: scalp mode + no T1 bank +
    # left_overbought forced off → winner only died on original stop / EOD).
    edge = str(cfg.get("ai_edge_mode") or "continuation").strip().lower()
    if edge in ("exhaustion", "exhaustion_scalp", "scalp", "ob", "overbought"):
        edge = "exhaustion_scalp"
    else:
        edge = "continuation"
    dual = bool(cfg.get("ai_day_scalp_dual_tranche", True))
    broker_tp = bool(cfg.get("ai_entry_broker_target", True))
    if "ai_exit_left_overbought" in cfg:
        left_ob = bool(cfg.get("ai_exit_left_overbought"))
    else:
        left_ob = edge == "exhaustion_scalp"
    if edge == "exhaustion_scalp" and not left_ob:
        out.append(
            "ai_edge_mode=exhaustion_scalp with ai_exit_left_overbought=false — "
            "%R band exit is off; winners only leave on hard stop / T1 / EOD"
        )
    if not dual and not broker_tp and not left_ob:
        out.append(
            "no dual scale-out, no broker T1, and left_overbought off — "
            "open longs have no upside bank path beyond the hard stop / EOD"
        )
    if dual and not broker_tp and not left_ob:
        out.append(
            "ai_day_scalp_dual_tranche on but neither broker T1 nor "
            "left_overbought will bank the scale-out leg"
        )

    return out


# Knobs an operator must be able to read off a log line. Defaults, bot_config,
# and commit messages have disagreed (continuation vs exhaustion_scalp vs
# "hybrid arm"); this is the resolved set the process is actually running.
_EFFECTIVE_KEYS = (
    "ai_edge_mode",
    "ai_stop_use_market",
    "ai_watch_synth_rr",
    "ai_min_reward_risk",
    "ai_daily_loss_limit_r",
    "ai_pdt_protect",
    "ai_max_open_risk_pct",
    "ai_max_positions",
    "ai_max_position_pct",
    "ai_risk_pct",
    "require_protective_exit",
    "ai_trading_source",
    "ai_trade_style",
    "ai_watch_zone_mode",
    "ai_eod_liquidate_time",
    "ai_entry_order_style",
)


def config_effective(cfg: dict | None = None) -> dict:
    """Resolved desk knobs: *cfg* if given, else load_config()."""
    src = cfg if isinstance(cfg, dict) else load_config()
    out = {}
    for k in _EFFECTIVE_KEYS:
        if k in src:
            out[k] = src[k]
        elif k in DEFAULT_CONFIG:
            out[k] = DEFAULT_CONFIG[k]
        else:
            out[k] = None
    return out


def format_config_effective(cfg: dict | None = None) -> str:
    """Single log line: ``k=v k=v ...`` in stable key order."""
    parts = []
    for k, v in config_effective(cfg).items():
        if isinstance(v, bool):
            parts.append(f"{k}={'true' if v else 'false'}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)


# Keys the dashboard API is allowed to update
SAFE_CONFIG_KEYS = [
    "api_key", "secret_key", "finnhub_key",
    "bar_timeframe", "bar_count", "scan_interval_sec",
    "rte_threshold", "rte_min_boxes",
    "cm_rsi_length", "cm_rsi_oversold",
    "obv_length",
    "macd_fast", "macd_slow", "macd_signal",
    "volume_surge_mult",
    "ema_short", "ema_long",
    "wr_length",
    "tv_chart_url",
    "brave_tv_tab",
    "tv_browser_macos",
    "strategy",
    "discord_ocr_poll_sec",
    "discord_window_owner",
    "discord_window_title",
    "mention_alert_threshold",
    "mention_alert_window",
    "push_vapid_public_key",
    "swing_screener_enabled",
    "swing_full_market",
    "swing_min_dollar_vol",
    "swing_max_price",
    "swing_rsi_oversold",
    "swing_min_eps_growth",
    "swing_max_eps_growth",
    "swing_min_rating",
    "swing_rel_vol_entry",
    "swing_rel_vol_min",
    "swing_min_rr",
    "swing_earnings_window_days",
    "swing_bar_lookback_days",
    "swing_run_times",
    "swing_limit",
    "swing_enrich_cap",
    "rs_screener_enabled",
    "rs_universe_source",
    "rs_cache_path",
    "rs_bar_adjustment",
    "rs_backfill_calendar_days",
    "rs_lookback_sessions",
    "rs_overlap_sessions",
    "rs_split_tolerance",
    "rs_ffill_limit",
    "rs_min_coverage",
    "rs_min_population",
    "rs_max_stale_frac",
    "rs_max_p0_staleness_sessions",
    "rs_form",
    "rs_benchmark",
    "rs_exclude_etp",
    "rs_chunk_size",
    "rs_incremental_chunk_size",
    "rs_max_req_per_min",
    "rs_use_partial_session",
    "rs_settle_after",
    "rs_run_times",
    "rs_limit",
    "rs_min_rs_rating",
    "rs_min_price",
    "rs_min_avg_vol_50d",
    "rs_require_above_sma50",
    "rs_require_above_sma200",
    "rs_use_rvol_filter",
    "rs_min_rvol",
    "rs_use_adr_filter",
    "rs_min_adr_pct",
    "ai_trader_enabled",
    "ai_trading_source",
    "ai_trading_enabled",
    "ai_max_price",
    "ai_quote_poll",
    "ai_volume_poll",
    "ai_avg_days",
    "ai_rvol_time_adjusted",
    "ai_trade_amount",
    "ai_max_positions",
    "ai_max_buys_per_poll",
    "ai_max_sells_per_poll",
    "ai_risk_pct",
    "ai_trade_style",
    "ai_min_reward_risk",
    "ai_positions_poll_sec",
    "ai_prompt_file",
    "ai_entry_unconfirmed_ttl_sec",
    "ai_daily_loss_limit_r",
    "ai_pdt_protect",
    "ai_max_open_risk_pct",
    "ai_open_bell_enabled",
    "ai_open_bell_time",
    "ai_eod_liquidate_enabled",
    "ai_eod_liquidate_time",
    "ai_sod_liquidate_enabled",
    "ai_duel_enabled",
    "ai_duel_close_before_research_min",
    "ai_duel_trial_end_time",
    "ai_duel_chance3_time",
    "ai_require_agreement",
    "ai_max_spread_pct",
    "ai_min_dollar_volume",
    "ai_watch_enabled",
    "ai_watch_start_time",
    "ai_watch_require_agreement",
    "ai_watch_single_source",
    "ai_watch_poll_sec",
    "ai_structure_ttl_sec",
    "ai_watch_expire_at_close",
    "ai_watch_seed_momentum",
    "ai_watch_seed_momentum_n",
    "ai_watch_seed_momentum_open",
    "ai_watch_seed_momentum_open_n",
    "ai_watch_seed_trending",
    "ai_watch_seed_trending_n",
    "ai_watch_trending_min_pct_change",
    "ai_watch_trending_min_rvol",
    "ai_watch_seed_research",
    "ai_watch_seed_research_n",
    "ai_watch_seed_bb_live",
    "ai_watch_seed_bb_live_n",
    "ai_watch_bb_live_fresh_sec",
    "ai_watch_trending_min_score",
    "ai_watch_min_pct_change",
    "ai_watch_min_rvol",
    "ai_watch_look_max",
    "ai_watch_require_look_ext",
    "ai_watch_synth_zone_enabled",
    "ai_watch_zone_mode",
    "ai_watch_require_db_zone",
    "ai_watch_armable_zone_kinds",
    "ai_edge_mode",
    "ai_exit_left_overbought",
    "ai_watch_exhaustion_rules",
    "ai_watch_exhaustion_live",
    "ai_watch_exhaustion_exit_sec",
    "ai_watch_exhaustion_exit_give_pct",
    "ai_watch_exhaustion_heat_min_pct",
    "ai_watch_exhaustion_fallback",
    "ai_watch_require_exhaustion_data",
    "ai_watch_exhaustion_max_window_mult",
    "ai_watch_db_bar_seconds",
    "rte_fast_ewm_span",
    "rte_direction_eps",
    "ai_watch_arm_below_zone",
    "ai_watch_arm_below_zone_max_r",
    "ai_watch_min_stop_pct",
    "ai_max_spread_r",
    "ai_watch_db_above_pct",
    "ai_watch_db_below_pct",
    "ai_watch_db_stop_below_pct",
    "ai_watch_db_match_pct",
    "ai_watch_db_swing_bars",
    "ai_watch_db_min_sep_bars",
    "ai_watch_db_lookback_bars",
    "ai_watch_db_bar_refresh_sec",
    "ai_watch_db_require_price_above",
    "ai_watch_zone_variable",
    "ai_watch_zone_top_pctl",
    "ai_watch_zone_bottom_pctl",
    "ai_watch_zone_top_min_pct",
    "ai_watch_zone_top_max_pct",
    "ai_watch_zone_bottom_min_pct",
    "ai_watch_zone_bottom_max_pct",
    "ai_watch_zone_min_width_pct",
    "ai_watch_zone_min_samples",
    "ai_watch_zone_dip_window_bars",
    "ai_watch_zone_time_decay",
    "ai_watch_zone_decay_floor",
    "ai_watch_zone_offset_pct",
    "ai_watch_zone_width_pct",
    "ai_watch_synth_stop_pct",
    "ai_watch_synth_reanchor_pct",
    "ai_watch_synth_rr",
    "ai_watch_synth_scale_out_pct",
    "ai_watch_synth_trail_pct",
    "ai_runner_trail_r",
    "ai_runner_step_r",
    "ai_day_scalp_dual_tranche",
    "ai_dead_trade_min",
    "ai_dead_trade_mfe_r",
    "ai_position_shadow_enabled",
    "ai_sell_signal_breakeven",
    "ai_heal_unprotected",
    "ai_adopt_unmanaged",
    "ai_flatten_unmanaged_unprotected",
    "ai_entry_broker_target",
    "ai_watch_require_uptrend",
    "ai_watch_require_indicators",
    "ai_watch_min_proximity",
    "ai_watch_arm_require_indicators",
    "ai_watch_arm_require",
    "ai_watch_arm_min_proximity",
    "ai_watch_min_adx",
    "ai_watch_min_price",
    "ai_watch_admit_ticks",
    "ai_watch_engine_push_max",
    "ai_watch_stream_enabled",
    "ai_watch_stream_max_age_sec",
    "ai_watch_stream_skip_margin_pct",
    "ai_max_position_pct",
    "ai_reentry_cooldown_sec",
    "ai_wash_cooldown_sec",
    "ai_entry_order_style",
    "ai_entry_limit_pad_pct",
    "ai_entry_limit_ttl_sec",
    "ai_stop_use_market",
    "ai_stop_limit_slip_pct",
    "ai_entry_zone_pad_pct",
    "ai_max_structure_calls_per_hour",
    "ai_persist_entry_decisions",
    "require_protective_exit",
    "ai_trading_host",
    "ai_shadow_log_enabled",
    "ai_reject_log_enabled",
    "momentum_max_tickers",
    "momentum_min_rvol",
    "realtime_symbol_budget",
    "claude_research_enabled",
    "claude_backend",
    "claude_cli_bin",
    "claude_model",
    "claude_effort",
    "claude_research_times",
    "claude_research_weekdays_only",
    "claude_research_catchup_min",
    "claude_request_timeout",
    "claude_live_search",
    "claude_search_tools",
    "claude_max_turns",
    "claude_max_output_tokens",
    "claude_use_prior_context",
    "claude_use_desk_snapshot",
    "claude_save_reports",
    "grok_research_enabled",
    "grok_trading_enabled",
    "grok_backend",
    "grok_cli_bin",
    "grok_model",
    "grok_max_turns",
    "grok_live_search",
    "grok_search_tools",
    "grok_use_prior_context",
    "grok_use_desk_snapshot",
    "grok_research_times",
    "grok_research_weekdays_only",
    "grok_research_catchup_min",
    "grok_prompt_file",
    "grok_request_timeout",
    "grok_save_reports",
    "grok_max_price",
    "trending_screener_enabled",
    "stocktwits_max_price",
    "trending_max_price",
    "stocktwits_poll",
    "stocktwits_quote_poll",
    "stocktwits_volume_poll",
    "stocktwits_stocks_only",
    "stocktwits_avg_days",
    "stocktwits_rvol_time_adjusted",
]


def _stamp() -> tuple:
    """(path, mtime, size) of both config files — cheap staleness check.

    The path is part of the key because tests monkeypatch CONFIG_FILE to a
    tmp dir: without it, two different config files that are both absent share
    a stamp, and the second test reads the first one's cache.
    """
    out = []
    for p in (CONFIG_FILE, SECRETS_FILE):
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return tuple(out)


# Memoized parse, keyed on the files' (mtime, size). load_config() is called
# per config key in places — _cfg_flag() reads one flag per call, inside the
# per-position, per-poll loops — so this used to reparse two JSON files
# thousands of times a session. A dashboard settings write changes the mtime,
# so a live edit is still picked up on the next read.
_cache: dict | None = None
_cache_stamp: tuple | None = None


def invalidate_cache() -> None:
    """Force the next load_config() to re-read from disk."""
    global _cache, _cache_stamp
    _cache = None
    _cache_stamp = None


def _copy(cfg: dict) -> dict:
    """Copy deep enough that a caller cannot reach into the cache.

    A plain dict() would still share the handful of list values (research
    times, arm_require, tickers), and a caller appending to one of those would
    corrupt the live config for every later reader until the process restarts.
    """
    return {
        k: list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v
        for k, v in cfg.items()
    }


def load_config() -> dict:
    """Resolved config: defaults <- bot_config.json <- secrets.json.

    Returns a fresh copy each call — several callers mutate the dict they get
    (ai_trader edits it in place), which would otherwise poison the cache.
    """
    global _cache, _cache_stamp

    stamp = _stamp()
    if _cache is not None and stamp == _cache_stamp:
        return _copy(_cache)

    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception as e:
            print(f"[CFG] Failed to load config ({e}) — using defaults")
    if SECRETS_FILE.exists():
        try:
            with open(SECRETS_FILE, "r", encoding="utf-8") as f:
                secrets = json.load(f)
            if isinstance(secrets, dict):
                for k in SECRETS_KEYS:
                    if k in secrets:
                        cfg[k] = secrets[k]
        except Exception as e:
            print(f"[CFG] Failed to load secrets ({e})")

    _cache, _cache_stamp = cfg, stamp
    return _copy(cfg)


def _write_json(path: Path, data: dict):
    """Save config, complaining rather than raising.

    A failed settings write must not take the dashboard down with it — the
    caller has no better recovery than to leave the old file in place.
    """
    try:
        desk_core.write_json_atomic(path, data)
    except Exception as e:
        print(f"[CFG] Failed to save {path.name}: {e}")


def save_config(cfg: dict):
    secrets  = {k: cfg[k] for k in SECRETS_KEYS if k in cfg and cfg[k]}
    main_cfg = {k: v for k, v in cfg.items() if k not in SECRETS_KEYS}
    _write_json(CONFIG_FILE, main_cfg)
    if secrets:
        _write_json(SECRETS_FILE, secrets)
    # Same-second writes can land on an unchanged (mtime, size) on coarse
    # filesystems, so drop the cache explicitly rather than trusting the stat.
    invalidate_cache()
