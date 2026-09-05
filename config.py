from __future__ import annotations
import json
import os
from pathlib import Path

import desk_core

CONFIG_FILE  = Path(__file__).parent / "config" / "bot_config.json"
SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"
SECRETS_KEYS = ["api_key", "secret_key", "finnhub_key",
                "push_vapid_private_key", "push_contact_email",
                "engine_control_secret", "desk_secret"]

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
    "rte_fast_length": 21,   # TV fast %R lookback
    "rte_fast_ewm_span": 7,  # TV fast smoothing
    "rte_slow_native_length": 112,  # TV slow %R on the same bars
    "rte_slow_ewm_span": 3,  # TV slow smoothing
    "rte_slow_timeframe": "",  # empty = native 112; "15min" is research-only
    "rte_confluence_max": 15.0,  # |fast-slow| for "close together"
    "rte_require_tight": True,   # red-box hold must also be tight to arm

    # ── Signal: CM RSI-2 ────────────────────────────────────
    "cm_rsi_length":    2,   # RSI period (2 = original Larry Connors CM RSI-2)
    "cm_rsi_oversold": 25,   # approaching-oversold threshold for signal
    "cm_rsi_buy_max":   10.0,  # line at the bottom of the CM RSI pane
    "cm_rsi_prefer_green": True,  # Connors color is a strength flag, not a gate
    # Desk buy = MACD bullish cross with wide line separation (gap).
    "ai_watch_tv_exh_rsi": False,

    # Live bar tape. iex is the free Alpaca feed. sip needs Algo Trader Plus
    # and is what matches TradingView highs/lows on thin names.
    "alpaca_bar_feed": "iex",

    # ── Signal: OBV Oscillator ──────────────────────────────
    "obv_length": 20,

    # ── Signal: MACD confirmation ───────────────────────────
    "macd_fast":   12,
    "macd_slow":   26,
    "macd_signal":  9,
    "macd_sep_mult": 0.8,
    "macd_min_gap": 0.005,
    "ai_watch_arm_require_macd": True,
    # Refuse to arm when the fast/slow gap is CLOSING. Every other MACD test
    # measures how far apart the lines are; none says which way they are
    # moving, so a +0.03 gap that was +0.08 two bars ago passes all of them
    # while the momentum it is meant to ride is already over. Judged on
    # trend_lookback (2 bars), the same basis as cm_rsi_rising.
    #
    # Independent of ai_watch_arm_require_macd: with require_macd off, EXH+RSI
    # arm the open and this knob alone still vetoes a closing gap (fail-open
    # when direction is unknown). With require_macd on, it runs last inside
    # the full size/bullish stack and unknown direction refuses.
    #
    # A FLAT gap still passes — the rule is "do not open into a closing gap",
    # and flat is not closing. False = shipped behaviour (size only).
    "ai_watch_macd_block_narrowing": False,
    # The other half of MACD direction, as its own veto: refuse to open when
    # the fast line is at or below the slow line (a negative histogram is the
    # same statement). Independent of ai_watch_arm_require_macd, fail-open on
    # missing MACD.
    #
    # Why it is separate from require_macd. That flag bundles direction with
    # SIZE (macd_min_gap, macd_sep_mult) and AVAILABILITY (no_macd_data,
    # macd_src_unknown, macd_stale_bars). Measured 2026-08-31..09-04 the
    # bundle refused 84-94% of every arm decision, macd_bearish the largest
    # single reason each session; turning it off to stop size and
    # availability starving opens drops the direction test with them. This
    # knob is the EXH+RSI arm path keeping "not crossed down" without the
    # rest. False = shipped behaviour.
    "ai_watch_macd_block_bearish": False,
    # Confluence override (operator, 8/26): MACD open and RISING at any gap
    # while %R exhaustion is RISING and at or past
    # ai_watch_macd_exh_override_min_pct arms the entry regardless of
    # macd_min_gap and the separation test. Two independent readings agreeing
    # is the evidence; gap size is not.
    #
    # BOTH must be turning up, not merely present: a %R at 85 that is rolling
    # over is a top, and the operator's own setup calls that "where the
    # profit gain stops". Runs before both size tests; cannot bypass the
    # bearish check, and cannot collide with the narrowing rule since it
    # requires the gap to be rising.
    "ai_watch_macd_exh_override":         False,
    "ai_watch_macd_exh_override_min_pct": 70.0,
    # Refuse a MACD the engine drew on the REST fallback instead of the
    # Finnhub tape, and optionally one whose bars are too old. MACD became
    # the entry lever on 8/26 with no provenance check while the levers it
    # replaced both had one (ai_watch_require_live_pctr,
    # ai_watch_require_realtime_rsi). Measured 8/26: realtime bars run 0.3s
    # at the median, the REST fallback up to 60s — an entry on the latter is
    # an entry on a different indicator.
    "ai_watch_require_realtime_macd":  False,
    # 0 = source check only, no age ceiling. Live desk sets 30s so a
    # "realtime" bar that has not printed in half a minute cannot arm.
    "ai_watch_macd_max_age_sec":       0.0,
    # Entry-only: refuse to arm/place unless decision_price / refresh_arm
    # market data returned px_src == "stream" (fresh tape). REST / stale_tape
    # / none are blocked as stream_required. Default False so unit tests and
    # paper configs keep existing rest-fallback behaviour; live bot_config
    # turns it on.
    "ai_watch_arm_require_stream_price": False,

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
    # Shared process/settings (ai_*). AGY slot still uses legacy claude_* keys.
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
    # Ratchet-first: give the 0.10R shelf more names to work. Fill abort and
    # cheap-OB still block dumps; selection is no longer the bottleneck.
    "ai_max_positions":            2,
    # One open slot per this much equity (floor 1, ceiling ai_max_positions).
    # $500 → 1 name; $1,000 → 2. Set 0 to disable scaling.
    "ai_position_slot_equity":   500.0,
    "ai_max_buys_per_poll":        1,
    "ai_max_sells_per_poll":       5,
    "ai_risk_pct":               1.0,
    # Size each new entry from leftover (unoccupied) equity across remaining
    # slots so a ~$250 / 3-slot book buys multiple shares of a $25–50 name
    # instead of the 1-share ticket 1% risk-of-equity produces. Slot COUNT
    # still comes from ai_position_slot_equity / ai_max_positions. False
    # restores the old risk-then-cap_long_qty path.
    "ai_size_from_free_equity":  True,
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
    "ai_max_position_pct":         8.0,     # max % of equity in one name
    # Names under ai_watch_cheap_price (HCTI/BYSI): tighter notional.
    "ai_max_position_pct_cheap":   5.0,
    "ai_watch_cheap_price":        5.0,
    "ai_reentry_cooldown_sec":    300.0,    # no re-arm this soon after an exit
    # After a wash-trade broker reject, freeze the symbol this long so the
    # poller does not re-place every 20s (2026-08-11 QMCO thrash).
    "ai_wash_cooldown_sec":      1800.0,
    # ── Entry order shape ───────────────────────────────────────────────────
    # "market" (desk default) or "limit". With broker stops ON, market means a
    # true market/bracket parent. With ai_broker_stop_enabled=false (local-stop
    # desk), "market" still submits a *marketable* DAY limit at send-ask×(1+pad)
    # — bare ask rests and misses on thin IEX (GLXY 2026-09-03). Limit style
    # pads then hard-caps at the zone top.
    "ai_entry_order_style":    "market",
    # Marketable pad above the ask. Limit style: then hard-capped at zone top.
    # Local-stop + market style: same pad, optionally dollar-capped via
    # ai_entry_marketable_pad_max_px (no zone cap — immediacy over geometry).
    "ai_entry_limit_pad_pct":     0.15,
    # Dollar cap on the marketable pad for local-stop market-style entries
    # (ask*(1+pad) never more than ask+this). 0 disables the dollar cap.
    "ai_entry_marketable_pad_max_px": 0.05,
    # An unfilled entry limit is cancelled after this long: if price left the
    # zone the setup is gone, and re-evaluating beats leaving a stale order
    # resting while the zone re-anchors away from it. Distinct from
    # ai_entry_unconfirmed_ttl_sec, which covers a *filled* but unconfirmed fill.
    "ai_entry_limit_ttl_sec":     30.0,
    # Atomic confirm→submit (Package B): refuse place if send-ask moved more
    # than this from the streak-pass print (pct OR absolute cents).
    "ai_entry_confirm_max_slip_pct": 1.0,
    "ai_entry_confirm_max_slip_px":  0.10,
    # True → stop-MARKET (default: gap through the trigger still fills).
    # False → stop-LIMIT with ai_stop_limit_slip_pct room; can miss entirely
    # on the high-RVOL names this book selects for.
    "ai_stop_use_market":         True,
    "ai_stop_limit_slip_pct":      1.0,     # room under trigger when stop-LIMIT
    "ai_entry_unconfirmed_ttl_sec": 900.0,  # cancel unfilled managed entries
    "ai_daily_loss_limit_r":        3.0,    # stop new entries after -NR today
    # Optional house day-trade throttle (legacy name). FINRA Rule 4210 PDT
    # designation and the $25k minimum were eliminated 2026-06-04 (Reg Notice
    # 26-10). Alpaca implemented the same day and deprecated daytrade_count.
    # "block" is NOT a legal requirement on live under $25k. Paper can still
    # report a leftover daytrade_count (198 on 2026-08-13) that blocks every
    # armed buy — leave this "off" unless you want a self-imposed cap.
    "ai_pdt_protect":            "off",     # block | warn | off
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
    # Above this, a logged spread_r is the IEX quote being wrong rather than a
    # wide book, and it stops sizing the trail. Measured against SIP on the
    # 2026-08-28 fills: the artifacts run 30-170x, every real reading 1.3-6x.
    # See ai_positions.DEFAULT_SPREAD_R_SANE_MAX. 0 = act on whatever the feed
    # said, which is the behaviour this replaces.
    "ai_spread_r_sane_max":         0.50,
    "ai_min_dollar_volume":         0.0,    # 0 = off; else require row dollar_volume
    # Entry watch poller (agreement queue + structure TTL / arming)
    "ai_watch_enabled":                 True,   # enable entry-watch queue
    # Weekdays: seed/sync AI Watch from this ET time until EOD liquidate.
    "ai_watch_start_time":           "04:00",   # ET — watch/shadow from premarket; buys still RTH + desk_product
    "ai_watch_require_agreement":      False,   # only watch AX-agreed names
    "ai_watch_single_source":          False,   # allow single-source watch when True
    "ai_watch_poll_sec":                20.0,   # watch poll interval (seconds)
    # Buy/sell may only use a stream print this young, or a REST ask fetched
    # on this poll. Older leftover last_ask values (FGI 11.69 vs tape 10.28)
    # must not arm or flatten.
    "ai_watch_decision_max_age_sec":     8.0,
    # If stream and REST stay dark this long in RTH, flatten every open long.
    "ai_stale_data_flatten":            True,
    "ai_stale_data_max_age_sec":        15.0,
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
    # Premarket session adapter. Default OFF — no live bid/ask yet (HANDOFF
    # §5G). When on, the current ratchet still decides; only the sell
    # transport changes (one DAY ext-hours limit) until 09:30 handoff.
    "ai_premarket_working_sell":       False,
    "ai_premarket_quote_max_age_sec":    15.0,
    "ai_premarket_max_exit_slip_r":      0.25,
    "ai_premarket_chase_step_sec":        2.5,
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
    "realtime_symbol_budget":             45,
    # Seed entry-watch from live desk heat (structure poller still defines levels).
    "ai_watch_seed_momentum":           True,
    "ai_watch_seed_momentum_n":           12,
    # Momentum Stocks panel → AI Watch with a soft path (no score/indicators).
    # RVOL + uptrend still apply when known. Prefers Stocktwits overlap.
    "ai_watch_seed_momentum_open":      True,
    "ai_watch_seed_momentum_open_n":      10,
    "ai_watch_seed_trending":           True,
    "ai_watch_seed_trending_n":           20,
    # Alpaca movers seed — the one source that is not sentiment. See
    # movers_screener.py for why the producer, not this poll, drops warrants
    # and floors the RVOL denominator.
    "ai_watch_seed_movers":             True,
    "ai_watch_seed_movers_n":             8,
    "ai_watch_movers_min_pct_change":   10.0,
    # Judge a movers row on a live quote rather than on whatever the producer
    # recorded. 0/false keeps the file's own price and pct_change, which go
    # stale the moment the producer does. rvol is never enriched either way.
    "ai_watch_movers_enrich":           True,
    # Refuse a movers file older than this rather than seed a stale ranking.
    "ai_movers_max_age_sec":           900.0,
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
    # Gates _big_mover_from_dashboard ONLY. The soft open seed
    # (mom_open_soft) has its own knob below and ignores this one.
    "ai_watch_min_pct_change":         50.0,  # day chg % for momentum big-mover seed
    # Soft open seed floor. Raised to 8 so mom_open_soft / mom+trending
    # paths need real heat (blocks HPE-class +4.8% late chase). 0.0 would
    # admit as originally shipped (median landed at +8.2%).
    "ai_watch_open_seed_min_pct":      8.0,
    # When the desk already has a young stream print, mom_open may use this
    # lower day-chg floor so curated heat is not dead on arrival waiting for
    # open_seed_min_pct. 0 = no stream shortcut (always use open_seed_min_pct).
    "ai_watch_open_seed_stream_min_pct": 5.0,
    # Same bar as momentum_min_rvol (dashboard watchlist). Known rvol below
    # this refuses AI Watch admission; unknown abstains (provisional).
    # Trending seed uses ai_watch_trending_min_rvol (default 1.5) instead.
    # Admission floor only. Arm uses ai_watch_arm_min_rvol (0 = zone+ratchet).
    "ai_watch_min_rvol":                2.0,
    # Movers SIP rvol runs lower than desk IEX; a shared 2.0 floor emptied
    # the movers shortlist on 2026-09-04 (BIAF/LABX wiped at 0.8–1.8x).
    "ai_watch_movers_min_rvol":         1.0,
    # Day-chg % that waives known-thin RVOL at seed + inclusion. 0 = off.
    "ai_watch_hot_move_rvol_waive_pct": 20.0,
    "ai_watch_arm_min_rvol":            0.0,
    # Credibility bound, not a heat ceiling. A relative-volume reading above
    # this is not a hot name, it is a broken number, and the desk must not
    # size a trade off one. Of the 24 at_last entries taken at rvol >= 8
    # through 2026-09-03, nineteen read between 26.8 and 1144.6 — the top of
    # that range clusters around 1000, which is an arithmetic fault, not a
    # tape. Those nineteen averaged -0.236R against -0.035R for the book as a
    # whole and cost -4.48R. The five plausible 8-20 readings are left alone:
    # this refuses what cannot be true, and takes no view on what is merely
    # extreme. 0 disables.
    "ai_watch_arm_rvol_sane_max":      25.0,
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
    # Maximum float in MILLIONS of shares. Finnhub profile2.floatingShare,
    # cached by float_feed. 0 = no float filter.
    #
    # 50 was the 2026-08-28 measurement cut (names >=50M were 438/507 trades
    # and reached +0.25R on 8% of them) and is TOO TIGHT for this desk: it
    # would have dropped BULL (532M) while the mega-floats occupying the
    # book on 2026-09-03 were HPE 1324 / SOFI 1290 / RIVN 1447. 800 keeps
    # BULL with margin and still refuses those three. Do not set 50.
    "ai_watch_max_float_m":       800.0,
    "ai_watch_min_price":               2.0,  # match movers band; no sub-$2 pennies
    # Seconds a name stays on the book after the panels stop offering it.
    # The book is rebuilt from each cycle's candidate list, so a marginal name
    # (rvol crossing 2.0, pct_change crossing zero) is dropped and re-added
    # repeatedly, losing its zone, its admit stamp and its arm streak each
    # time. ai_watch_arm_confirm_ticks needs consecutive polls, which such a
    # name can never accumulate. 0 = rebuild strictly from candidates.
    "ai_watch_admit_grace_sec":     0.0,
    "ai_watch_admit_ticks":               1,  # consecutive qualifying polls to admit
    # Consecutive stale-tape polls before a watch is dropped for having no
    # quote feed at all. A name IEX cannot quote can never arm, but it still
    # spends a book slot, a poll and REST budget every cycle, and it reads to
    # the operator as a setup that might fire — 4 of 11 rows on 2026-08-31,
    # some carrying quotes days old (NCRA 239,722s). The streak resets on any
    # usable quote, so a thin name pausing between prints is never evicted for
    # it; only a name the feed cannot price at all runs the count up.
    # 0 disables the drop.
    "ai_watch_stale_tape_drop_polls":     0,
    # Wall-clock eviction for watches stuck on *dead* stale_tape with no
    # young trade_ts. Poll-count above ships off; this is the live default
    # (~6 min RTH). 0 disables. Open positions are never dropped. By default
    # brief need-stream after admit does NOT count (subscribe lag).
    "ai_watch_stale_timeout_sec":         360.0,
    # Seconds on-book before the stale_timeout clock may start (Finnhub
    # subscribe grace). 0 = start immediately.
    "ai_watch_stale_timeout_grace_sec":   90.0,
    # After a stale_timeout drop, refuse re-seed for this long *while the
    # tape is still dead*. A young stream print clears the cool immediately.
    # 300s default (was 1800 — starved selection on 2026-09-04). 0 = off.
    "ai_watch_stale_timeout_reseed_sec":  300.0,
    # When true, need-stream/rest also advances the stale_timeout clock.
    # Default false — early RTH subscribe lag must not burn the admit window.
    "ai_watch_stale_timeout_include_need_stream": False,
    # Known WS/engine print younger than this is quiet tape, not dead — do
    # not start the stale_timeout drop clock. 180s (was 900): AEHG/AOUT/LABX
    # parked for 10–14 min of dated-but-dead book prints as false opportunity.
    "ai_watch_stale_timeout_quiet_max_sec": 180.0,
    # After admit + subscribe grace, if no young stream print for this long,
    # drop the watch (Finnhub subscribed but never trades / tape went dark).
    # 0 disables. Default 5 min.
    "ai_watch_no_trade_after_subscribe_sec": 300.0,
    # Reseed cool after a no_stream_trade drop (longer than generic stale_timeout
    # reseed so thin +20% micro names do not bounce straight back onto the book).
    # 0 falls back to ai_watch_stale_timeout_reseed_sec.
    "ai_watch_no_trade_reseed_sec": 900.0,
    # Refuse admit when live tape is missing or older than this (seconds).
    # Prefer an empty slot over a permanent stale_quote row. 0 disables.
    "ai_watch_admit_max_tape_age_sec": 120.0,
    # Movers seats: liquidity floors so micro-float no-trade names do not crowd
    # out stream-ready trending / higher-$vol names. 0 = off for that floor.
    "ai_watch_movers_min_dollar_volume": 2_000_000.0,
    "ai_watch_movers_min_price": 5.0,
    # Stricter tape age for movers admit (seconds). 0 = use admit_max_tape_age.
    "ai_watch_movers_admit_max_tape_age_sec": 60.0,
    # Max watching rows that may sit on stale_tape at once. Excess dropped
    # (lowest $vol / oldest first). 0 = no stale_tape seats; <0 = unlimited.
    "ai_watch_max_stale_tape_seats": 2,
    # Post-admit Finnhub subscribe grace: paint await_stream instead of sticky
    # need-stream, and re-assert WS sub. Defaults to stale_timeout_grace_sec.
    "ai_watch_stream_subscribe_grace_sec": 90.0,
    # ── Real-time tape pre-filter ───────────────────────────────────────────
    # The Finnhub WebSocket price (via the dashboard's ticker rows) is used to
    # SKIP the per-symbol Alpaca quote when price is nowhere near the zone.
    # It is never used to arm: the socket carries trades, not quotes, and a
    # print at the bid would arm on a price the order cannot actually get.
    # Max symbols we push into the signal engine for indicator computation.
    # Finnhub's free tier allows ~50 concurrent WS subscriptions desk-wide;
    # request_subscribe enforces the ceiling, but keep push ≤ engine capacity.
    "ai_watch_engine_push_max":          32,
    "ai_watch_stream_enabled":         True,
    "ai_watch_stream_max_age_sec":     10.0,  # older than this → fall back to REST
    "ai_watch_stream_skip_margin_pct":  1.0,  # only skip when this far outside
    # Synthetic pullback zone when model has no levels (Mom/ST).
    "ai_watch_synth_zone_enabled":     True,
    # Zone construction mode for synthetic levels:
    #   "pullback"      — buy band from this name's own dip history (default).
    #   "double_bottom" — two matching swing lows on 1m bars; tiny band above
    #                     support. Most momentum names never print this.
    #   "offset"        — fixed % under the live print (legacy; not armable).
    "ai_watch_zone_mode":     "pullback",
    # How the book arms a buy.
    #   "zone" — wait for a pullback into the band (capital-first default).
    #   "last" — buy the tape; RSTOP is the trade. Zone is display/R only.
    "ai_watch_arm_mode":              "last",
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
    # Live money path. observe = no new auto-arms (docs/PROFIT_REDESIGN.md).
    # Partial test cfg dicts that omit the key still behave as scalp_legacy.
    "desk_product":              "observe",
    "ai_h4_paper":               False,
    "ai_h3_paper":               False,
    "h4_min_price":              10.0,
    "h4_min_dollar_vol":         5_000_000.0,
    "h4_max_spread_pct":         0.10,   # percent of mid; round trip ≈ 20 bps
    "h4_min_rs":                 80.0,
    "h4_hold_days":              2,
    "h4_stop_pct":               2.0,
    "h4_haircut_pct":            0.20,   # vs cash, same units as desk_null

    # Explicit override for left_overbought software exit. None/absent → follow
    # edge mode (on only for exhaustion_scalp). Set false to force off.
    # "ai_exit_left_overbought": False,
    # Exhaustion / %R is a *direction* filter, not a heat floor.
    # BUY: %R rising, or already overbought and not falling.
    # Refuse cooling / rolling-over OB. Missing %R still passes when
    # require_data is false (thin tape).
    "ai_watch_exhaustion_rules":      True,
    # Recompute %R against the live price instead of trusting the engine's
    # 60-120s-old copy. Closed bars give the window, the live quote gives the
    # close — no new market data, just no waiting for a bar to close.
    "ai_watch_exhaustion_live":       True,
    # The close that recompute uses must be a TRADE, never the ask.
    #
    # The decision price falls back to a REST ask whenever the tape goes
    # quiet, and the live price is folded into the window high
    # (max(bar_highs + [px])), so an ask above the window's range becomes the
    # high and %R lands on exactly -0.0 — EXH 100, "overbought" — wherever the
    # stock actually is. The error is one-directional: an ask can only raise
    # the high, so the fallback manufactures overbought and never oversold.
    #
    # 2026-08-20 09:50: RARE published EXH 100 off a 28.50 ask against a
    # 26.46 tape while the chart read 5 (near oversold); TEM published EXH 100
    # off a 68.13 ask against a 66.15 tape. BMNR, whose print came from the
    # stream, was correct at 92.4. Exact -0.0 / -100.0 in the column is the
    # signature: the price itself set the window extreme.
    #
    # False restores the old behaviour (draw %R on whatever price arrives).
    "ai_watch_exhaustion_trade_price_only": True,
    # EXH and CM RSI-2 on sampled Finnhub tape minutes (stream_bars) spliced
    # over the Alpaca IEX seed. IEX alone is ~0.6 bars/min on these names and
    # a 21-minute %R becomes an hour of range. The watch poll already sees
    # the print every ~2s; this is that print as 1-minute OHLC. Off restores
    # REST-only windows. Does not change the min-hold exit.
    "ai_watch_stream_bars_live": True,
    # Prefer engine %R (Finnhub realtime_bars) when the wire is this fresh.
    "ai_watch_engine_exh_max_age_sec": 8.0,
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
    # 0 = any rising %R may arm. The 50 floor blocked UMAC at 37% in-zone.
    # 20–50 + rising (2026-08-17 tape): from-OS polls +1.45%/30m; 65+ heat
    # and OB fills were the loss bucket.
    "ai_watch_exhaustion_heat_min_pct": 20.0,
    # Refuse to arm once %R is already this extended (0–100).
    # 0 = no cap. A 50 ceiling parked AMLX (EXH ~86) as "extended"
    # while it was in/below the zone; operator wants those fills.
    "ai_watch_exhaustion_heat_max_pct": 0.0,
    # Require gaining EXH (pctr_rising) to arm. Falling EXH → exh_falling;
    # flat → exh_not_rising (except pinned-ceiling + MACD-armed). Missing
    # reading → exh_rising_required (no blind no_exhaustion_fallback).
    # Intent: more early opens, fewer late chases. RSI hard max stays 60;
    # mistimed_heat + soft_ob still separate GTLB/HPE from BULL (RSI 46 OB).
    "ai_watch_require_exh_rising": True,
    # Soft overbought / late-heat arm veto. Refuse when the name is already
    # in the overbought band AND RSI is at/above this floor (still below the
    # hard RSI max). Separates HPE-class (RSI 59.6 + EXH 83.5 OB, −0.10R)
    # from BULL-class (RSI 46.3 + EXH 85 OB, +0.53R) on 2026-09-03. A blunt
    # heat_max ~80 would have killed both. 0 / enabled false = off. Does
    # not change RSI max 60, macd_min_gap, or the EXH override.
    "ai_watch_soft_ob_enabled": True,
    "ai_watch_soft_ob_rsi_min": 55.0,
    # Mistimed heating-band chase (GTLB 2026-09-04). Soft OB only covers
    # overbought+RSI≥55; a name still in the heat band with mid/high RSI
    # used to arm (confirm RSI ~59, pass 53.3 → MFE ~0.01R). Heating-only:
    # BULL-class last_overbought + RSI 46 stays on soft OB and is untouched.
    # Pass floor 52 blocks GTLB's 53.3; peak floor 55 is the backup when RSI
    # dips under the pass floor after printing hot on earlier confirm ticks.
    # Default ON for paper scalp_legacy. Does not change RSI hard max 60,
    # soft OB, macd_min_gap, or the EXH override.
    "ai_watch_mistimed_heat_enabled": True,
    "ai_watch_mistimed_heat_rsi_min": 52.0,
    "ai_watch_mistimed_heat_rsi_peak_min": 55.0,
    # Trending / momentum names already in the overbought band may still arm
    # (in or below the zone). Research stays on the 90-cap.
    "ai_watch_ob_allow_hot":          False,
    # Let a %R pinned at the top of its range stand in for a rising one,
    # but only while MACD is bullish AND opening. Williams %R is
    # position-in-range: at 100% it cannot rise, so pctr_rising and
    # pctr_falling are both False and the gate refuses it forever. Measured
    # 2026-08-27: CRMG 100.0%, CSIQ 100.0%, FIG 98.9% all flat and all
    # refused, on a day the desk was hunting momentum — the strongest names
    # were the only structurally unreachable ones. A FALLING %R is still
    # refused, so a top that has rolled over cannot get in this way.
    "ai_watch_ob_allow_flat_when_macd_armed": False,
    # How close to the ceiling %R must be for the flat-OB exemption to apply.
    # The exemption is for readings that CANNOT rise (100% is the top of the
    # range); anything lower is flat by choice of the tape and refusing it is
    # correct. GAP armed at 80.7% before this existed.
    "ai_watch_ob_flat_min_pct":        99.0,
    # Consecutive polls that must agree before a buy is placed. MACD lives on
    # the FORMING minute bar and flickers inside it; the hard sell has
    # required ai_exit_macd_confirm_ticks agreeing reads since 2026-08-28 and
    # the entry required one, so the desk bought noise and sold signal.
    "ai_watch_arm_confirm_ticks":      1,
    # Round RVOL into buckets before ranking entry candidates, so EXH and
    # MACD trend decide between names the tape is treating alike. 0 = exact
    # RVOL ordering, which leaves the signal legs almost never consulted.
    "ai_watch_rank_rvol_band":         0.0,
    # Rank entry candidates by how far they are actually MOVING first, in
    # percent, bucketed to this width. The one seat per poll should go to a
    # name that travels: the shelf trails 0.25% behind price, so a trade whose
    # whole move is 0.2% cannot finish above its own fill. RVOL does not
    # measure that. 0 = off, leaving RVOL to lead.
    "ai_watch_rank_move_band":         0.0,
    # Three strikes: a symbol round-tripped this many times today is done,
    # whatever the tape says. The re-entry cooldown only spaces attempts out;
    # it never stops them. 0 = no cap.
    "ai_watch_max_entries_per_symbol_day": 0,
    # False: cooling EXH refuses even at last / in-zone. Last-mode still
    # buys rising or pinned-OB names above the old pullback band.
    "ai_watch_in_zone_ignore_fade":  False,
    # Seconds after first in-zone print to wait for EXH to arm. Zone is the
    # trigger; rising EXH is the confirm. 0 = both must be true on the same tick.
    "ai_watch_zone_exh_window_sec":  20.0,
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
    # False: thin tape may still arm on zone; the ratchet does not need %R.
    "ai_watch_require_exhaustion_data": True,
    # CM RSI-2 entry filter, as its own gate rather than a member of the
    # ai_watch_arm_require triple (that list demands cm_ok AND pctr_ok AND
    # cm_rsi_rising together, and blocked every in-zone arm when last tried).
    #
    # Operator's rule: anything trending up from 0 to 50 is a good entry,
    # never trending down. A level test plus a direction test — so both have
    # to come off the same RSI series, which is what ai_watch_cm_rsi_local
    # below is about.
    "ai_watch_arm_require_cm_rsi":  False,
    # The band. 50 is the operator's ceiling; above it the entry is chasing.
    "ai_watch_arm_cm_rsi_max":       50.0,
    "ai_watch_arm_cm_rsi_min":        0.0,
    # Require the turn as well as the band. The band is the load-bearing half
    # — see the numbers in cm_rsi_allows_buy. False trades a little edge per
    # trade for five times the opportunities.
    "ai_watch_arm_cm_rsi_require_rising": True,
    # When rising is required: still allow a falling RSI if it is below this
    # level AND fast %R is already rising toward overbought (pctr_rising).
    # 0 = off (strict "never trending down"). 20 = deep washout exception.
    "ai_watch_arm_cm_rsi_allow_falling_below": 0.0,
    # Refuse an RSI the engine drew on the REST fallback instead of the
    # Finnhub tape. bars_src flips per ticker mid-session (20 recoveries and
    # 27 fallbacks across 18 symbols on 2026-08-20), so without this the same
    # gate silently alternates between two data sources. The %R side's
    # equivalent is ai_watch_require_live_pctr.
    "ai_watch_require_realtime_rsi": False,
    # Recompute CM RSI-2 locally off Alpaca IEX REST bars, overwriting the
    # engine's reading. False keeps ONE series: the level and the direction
    # both come from the engine, which with REALTIME_BARS on is the Finnhub
    # trade stream. True was the old behaviour and split them — the level from
    # a local recompute, cm_rsi_rising / cm_ok still the engine's — which is
    # not a pairing any "in the band and turning up" rule can be built on.
    # 2026-08-20: BMNR read 5.5/low=True on the wire and 20.1/low=False in the
    # book at the same second. The local path also has no clock window at all,
    # unlike live_exhaustion, so its closes can span the overnight gap.
    "ai_watch_cm_rsi_local":        False,
    # Only arm on a %R that is a real rolling reading (pctr_src == "live").
    # clock_range / sparse_window print in the same column and are a
    # different measurement. 0/False keeps the old behaviour.
    "ai_watch_require_live_pctr":   False,
    # The engine's %R is the only %R. Without this the desk falls back to a
    # LOCAL recompute over a different window whenever the engine's bars are
    # not realtime-fresh — a different indicator, not fresher data. They
    # disagreed by 48 points on AREN 2026-08-28 while MACD beside them came
    # from the engine.
    "ai_watch_exhaustion_engine_only": False,
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
    "ai_watch_db_lookback_bars":        220,  # 112-bar slow %R + SMA(200) room
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
    # First bank at 1R. 0.6R was a scalp that cut continuation winners
    # before the ratchet could lock them (30m+ holds were the only green
    # bucket on 2026-08-11..18).
    "ai_watch_synth_rr":               1.0,  # target at this R multiple
    # Scale-out / runner (synthetic dual tranche when ai_day_scalp_dual_tranche).
    "ai_watch_synth_scale_out_pct":   50.0,  # % of shares with T1 take-profit
    # Runner trail after T1, in R — a percent trail is a different trade on
    # every name (2.5% is 2.5R behind a 1% stop, 0.5R behind a 5% one), which
    # let the runner lose more than tranche A had just banked. The stop is
    # floored at breakeven, so a trade that reaches T1 cannot finish red.
    "ai_runner_trail_r":               1.0,
    # Local profit trail: stop = last − give, only ratchets up.
    # Armed only after +arm_r MFE so the structure stop owns the open.
    # 0.10R from tick one shook out 60 names last week at mean MFE 0.19R.
    # Flatten if last prints through. This is the stop of record when
    # ai_broker_stop_enabled is False — broker buys may sit naked.
    "ai_local_trail_enabled":         True,
    "ai_local_trail_arm_r":            0.5,
    # ...or this much percent of price, whichever comes first. Same reason
    # as be_at_pct: 1R is ~5% of price here, so an R-only arm freezes the
    # shelf through moves that are plainly real.
    "ai_local_trail_arm_pct":       0.0,
    # Initial stop, split from the trail give. 0 = use the trail give, which
    # is how this shipped and changes nothing. Above 0 it sets ONLY the shelf
    # at fill; the trail keeps give_r. See initial_local_stop for why the two
    # were never separable and why that made the question unmeasurable.
    "ai_local_trail_initial_give_r": 0.0,
    "ai_local_trail_give_r":           0.05,
    "ai_local_trail_give_open_r":      0.05,
    "ai_local_trail_tighten_mfe_r":    0.25,
    "ai_local_trail_give_px":          0.0,
    # Ceiling on the local trail cushion, percent of price. 0 = off.
    # Guards wide-R names: give_r x R is huge in dollars when the zone put
    # the stop 5% under entry, and the shelf stops being close support.
    "ai_local_trail_give_max_pct":  0.0,
    # Prints the shelf damps over before it raises. 3 = up to two extra
    # polls of lag on a new high; 2 halves that and still needs a second
    # print to agree, so one IEX spike cannot lift the shelf.
    "ai_local_trail_print_ring":    3,
    # Once MFE reaches this, the local shelf never sits under the fill
    # again. 0 = off. The runner stop already floors at breakeven; this is
    # the same guarantee for the pre-scale-out shelf.
    "ai_local_trail_be_at_r":       0.0,
    # ...or this much percent of price, whichever comes first. 1R is ~5% of
    # price on these zones, so an R-only floor needs a half-percent move.
    "ai_local_trail_be_at_pct":     0.0,
    # Breakeven parks this many cents ABOVE the fill, not on it — flat on
    # paper is red after paying the spread twice. 0 = old flat-at-fill.
    "ai_breakeven_offset_px":          0.01,
    # Book-thread tick: how often the heavy pass runs (publish, fills, T1,
    # dead-trade, EOD) — one get_positions_detail each time.
    "ai_book_tick_sec":                2.0,
    # Shelf tick: how often the rstop alone re-reads the tape and ratchets.
    # No broker call, so this can be far shorter than the book tick. 0 folds
    # the shelf back into the book tick (the old behaviour).
    "ai_shelf_tick_sec":               0.25,
    # Seconds of tape under the median that lifts the shelf. Sized in time so
    # the spike guard is worth the same at any tick rate.
    "ai_local_trail_damp_sec":         2.0,
    # Trail width as a multiple of the round-trip spread. 0 = off.
    # A cushion narrower than the book is not a stop: on 2026-08-21 the
    # shelf sat $0.06 behind price against an $0.08-0.18 book, so the quote
    # crossing its own spread tripped it without the market moving. Set from
    # the spread record, not guessed.
    # Where the entry limit is anchored: "ask" (marketable, pays the whole
    # book and opens every fill down by the spread), "mid" (half), or "bid"
    # (pays nothing, fills only when someone comes to it). Crossing buys
    # immediacy, which is worth its price only if the signal continues.
    # A REST ask further than this from the last print is disbelieved rather
    # than used: on thin names the quote ran 8-13% above the tape, which put
    # the derived stop ABOVE the live price and inflated every spread reading
    # taken from it. Percent of tape. 0 disables.
    "ai_decision_ask_max_dev_pct":      5.0,
    "ai_entry_limit_anchor":           "ask",
    # Discretionary exits (shelf, dead-trade, left-overbought) stay holstered
    # for this many seconds after the fill. The 1R disaster stop and the 15:50
    # flatten are never gated. 0 = shipped. See ai_positions.soft_exit_held_back.
    "ai_exit_min_hold_sec":            0,
    # MACD curled bearish on an OPEN position: pull the ratchet shelf to a
    # penny under the print so the next tick down exits. The entry thesis is
    # "the lines are separating"; a curl back together is that thesis
    # expiring, and the give was sized for noise rather than for a signal
    # that has already turned. Expressed as a floor on the wanted stop, so
    # the raise-only rule still holds: this tightens, never loosens.
    "ai_exit_macd_curl_tighten":       False,
    # LIQUIDATE on a MACD thesis break — flatten outright rather than pull
    # the shelf under the print (curl_tighten above). Two hard sells, both
    # skip min-hold: the gap turning NEGATIVE (the lines crossed; direction
    # does not matter), and a still-positive gap CLOSING under
    # ai_exit_macd_hard_sell_sep. A wide positive gap that is merely
    # falling is not a flatten — the trail owns that. Requires
    # macd_src == "realtime": a reading on the REST fallback is older bars,
    # and absence is not a pass. Off by default; this closes positions.
    "ai_exit_macd_liquidate":          False,
    # HARD sell: macd_sep_ratio under this while the gap is CLOSING. The
    # ratio is the gap measured in standard deviations of its own histogram,
    # so under 1.0 the separation is inside the noise the entry was meant to
    # clear; falling on top of that is small and shrinking. Rising, even
    # when thin, is left alone.
    "ai_exit_macd_hard_sell_sep":      1.0,
    # How many consecutive evaluations a hard sell must survive before it
    # fires. MACD is computed on the FORMING minute bar, so its gap moves with
    # every trade and the falling flag flips inside a single bar; one reading
    # is noise sampled at the positions tick. At a 3s tick, 3 is ~9s of
    # agreement. 1 = the old single-reading behaviour.
    "ai_exit_macd_confirm_ticks":      1,
    # Shelf trace: one log line per symbol per N seconds showing want vs the
    # stored shelf and whether the raise fired. Diagnostic only; 0 = off.
    "ai_shelf_trace_sec":              0.0,
    # A position flagged closing whose close returned no order id is stranded:
    # still open, still held, and skipped by the ratchet. Clear the flag after
    # this many seconds so it returns to management and the exit retries.
    # 0 = never clear (the old behaviour, which strands it forever).
    "ai_stranded_close_sec":           30.0,
    # Unused: both MACD liquidate reasons skip min-hold on their own.
    # Kept so existing bot_config keys still load.
    "ai_exit_macd_liquidate_ignore_hold": False,
    # How far under the print. One tick.
    "ai_exit_macd_curl_px":            0.01,
    # Also fire while the gap is still POSITIVE but closing. Earlier and
    # noisier: one narrowing bar inside a live move would flatten a winner
    # that was still working. Off unless the tape says otherwise.
    "ai_exit_macd_curl_on_falling":    False,
    "ai_local_trail_give_spread_k":    0.0,
    # Ceiling on the spread floor, in R. Uncapped, k=1 on a p90
    # book (5.56R) parks the shelf 5.5R down, which is no stop.
    "ai_local_trail_give_spread_max_r": 0.50,
    # Breakeven floor may not arm until the trade has cleared this many
    # round trips. 0 = off. be_at_pct alone armed on a third of the spread
    # and pinned the shelf a cent over entry on 45% of raises.
    "ai_local_trail_be_at_spread_k":   0.0,
    "ai_local_trail_min_give_px":      0.06,
    # Dollar floor may not exceed this many R. $0.06 on a $3 last-mode
    # name is 0.4R; cap keeps the 0.10R identity.
    "ai_local_trail_min_give_max_r":   0.20,
    # Abort a confirm when fill or tape is this far (R) through the limit/stop.
    # 0.30 let FGI/SPAI/TDIC open 2R in the hole on a stale ask (08-14).
    "ai_fill_abort_r":                 0.15,
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
    "ai_dead_trade_min":               30.0,
    "ai_dead_trade_mfe_r":            0.10,
    # Paper experiment: last-hour hold (gate 1+2, 2026-08-20). When on,
    # daytime auto-arm is off. New entries only 14:00–15:30 ET on names
    # admitted in that window; 2% hard stop, no 0.10R shelf, 30m dead,
    # 15:50 flatten. Default off so a checkout does not silently kill the
    # daytime scalp.
    "ai_late_hold_paper":              False,
    "ai_late_hold_start":              "14:00",
    "ai_late_hold_end":                "15:30",
    "ai_late_hold_stop_pct":           2.0,
    "ai_late_hold_dead_trade_min":     30.0,
    # After a losing exit that never printed 0.5R MFE, do not re-arm that
    # symbol for the rest of the ET session. Off by default: a same-day
    # trade is not a lifetime ban — if the name still passes inclusion
    # and cooldown, Bro may take it again. Winners / 0.5R runners were
    # already allowed when this flag is on.
    "ai_dead_reentry_block":           False,
    "ai_reentry_min_mfe_r":            0.50,
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
    # Local ratchet is the stop of record. Do not slap a broker stop onto a
    # naked fill (08-13: 185 heal failures vs 3 heals). Set True to restore
    # the old broker-heal loop. Local-stop missing-shelf heal/flatten
    # (unprotected_local) is independent of this flag.
    "ai_heal_unprotected":           False,
    # False = parent buy is a bare limit; local trail / dead_trade flatten.
    # Do not flip True overnight: broker OTOCO is a real architecture fork.
    "ai_broker_stop_enabled":        False,
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

    # Google AGY research source — legacy key prefix claude_* (do not reintroduce Claude).
    # Google AGY research slot (legacy key prefix claude_* — do not reintroduce
    # Claude Code as the default). Prefer agy / gemini via these keys or agy_*.
    # Duel marks: G (AGY) vs X (Grok).
    "claude_research_enabled":   False,
    "claude_backend":       "agy",
    "claude_cli_bin":          "agy",
    "claude_model":            "gemini-3-pro-high",
    "claude_effort":            "high",  # agy: low|medium|high
    "agy_backend":             "agy",
    "agy_cli_bin":             "agy",
    "agy_model":               "gemini-3-pro-high",
    "claude_research_times": ["08:30", "11:30", "14:30"],
    "claude_research_weekdays_only": True,
    "claude_research_catchup_min": 120,
    # Seed-only AI ranker (momentum+trending+movers → ≤5 names → watchlist).
    # Google AGY + Grok; recommend only; agreement required; never places.
    "ai_seed_rank_enabled": False,
    "ai_seed_rank_times": [
        "09:25",
        "10:00", "11:00", "12:00", "13:00", "14:00",
        "15:00",
    ],
    "ai_seed_rank_weekdays_only": True,
    "ai_seed_rank_catchup_min": 45,
    "ai_seed_rank_max": 5,
    "ai_seed_rank_agy": True,
    "ai_seed_rank_claude": True,  # legacy alias for ai_seed_rank_agy
    "ai_seed_rank_grok": True,
    "ai_seed_rank_require_agreement": True,  # both models must list the name
    "ai_seed_rank_require_setup": False,  # mechanical stage-1 pre-filter
    "ai_seed_rank_max_shares_m": 30.0,
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
    "movers_screener_enabled":   False,   # launch movers_screener.py
    "ai_movers_poll":             60.0,   # seconds between movers passes
    "ai_movers_poll_idle":       900.0,   # ...outside 04:00-20:00 ET
    "ai_movers_top":                50,   # gainers requested per pass
    "ai_movers_min_pct_change":   10.0,
    "ai_movers_min_price":         2.0,
    "ai_movers_max_price":        20.0,
    # Liquidity floor on TODAY's dollar volume — the question a screener
    # actually needs answered. Not on the 20-day average: a small average is
    # what makes RVOL interesting (QNRX did 31M shares against a 24k mean and
    # $190M of dollar volume on 2026-08-28), not what makes a name unsafe.
    "ai_movers_min_dollar_vol": 1000000.0,
    # Tape CONTINUITY, which the dollar floor above cannot see because a daily
    # sum cannot tell a name that trades every minute from one that does its
    # whole day in three bursts. Measured 2026-08-28: RDIB cleared $12.4M and
    # still had a 54-minute hole; YDES traded in 29% of RTH minutes. Both
    # passed the sum. Require this share of a trailing window's minutes to
    # have printed at least ai_movers_min_minute_dollars. 0 = off.
    "ai_movers_min_live_pct":         0.80,
    "ai_movers_live_window_min":        60,
    "ai_movers_min_minute_dollars": 2000.0,
    # Below this many OPEN minutes in the window there is not enough tape
    # to judge continuity, so the filter forms no opinion rather than
    # refusing everything — which is what it did in premarket.
    "ai_movers_live_min_open_minutes": 20,
    # Carry a name seen on the movers list earlier today even after the top-50
    # ranking evicts it. It is re-measured and re-gated every pass, so it stays
    # only while it still qualifies — losing a ranking contest is not failing a
    # rule. Cleared when the ET day turns over.
    "ai_movers_session_append":       True,
    "ai_movers_session_max":            40,
    # A second feed ranked by VOLUME. The gainers list is capped at 50 and its
    # weakest member was +15.0% on 2026-08-28 — above the desk's 10% floor — so
    # the 10-15% band is invisible to it. 0/false to use gainers only.
    "ai_movers_use_most_actives":     True,
    "ai_movers_actives_top":            50,
    "ai_movers_max_rows":           25,   # enrich at most this many per pass
    "ai_movers_float_refresh_per_pass": 10,  # bounded so the loop stays responsive
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
    push_max = int(cfg.get("ai_watch_engine_push_max", 32) or 0)
    mom_max = int(cfg.get("momentum_max_tickers", 8) or 0)
    budget = int(cfg.get("realtime_symbol_budget", 45) or 0)
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

    start = _hhmm("ai_watch_start_time", "04:00")
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
            "%R band exit is off; winners leave on local trail / T1 / EOD"
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
    "ai_position_slot_equity",
    "ai_max_position_pct",
    "ai_max_position_pct_cheap",
    "ai_watch_cheap_price",
    "ai_risk_pct",
    "ai_size_from_free_equity",
    "require_protective_exit",
    "ai_broker_stop_enabled",
    "ai_heal_unprotected",
    "ai_local_trail_enabled",
    "ai_premarket_working_sell",
    "desk_product",
    "ai_h4_paper",
    "ai_h3_paper",
    "ai_late_hold_paper",
    "ai_local_trail_arm_r",
    "ai_watch_arm_mode",
    "ai_watch_exhaustion_heat_max_pct",
    "ai_watch_require_exh_rising",
    "ai_watch_soft_ob_enabled",
    "ai_watch_soft_ob_rsi_min",
    "ai_watch_mistimed_heat_enabled",
    "ai_watch_mistimed_heat_rsi_min",
    "ai_watch_mistimed_heat_rsi_peak_min",
    "ai_watch_ob_allow_hot",
    "ai_watch_ob_allow_flat_when_macd_armed",
    "ai_watch_ob_flat_min_pct",
    "ai_watch_arm_confirm_ticks",
    "ai_watch_arm_rvol_sane_max",
    "ai_watch_rank_rvol_band",
    "ai_watch_rank_move_band",
    "ai_watch_max_entries_per_symbol_day",
    "ai_watch_zone_exh_window_sec",
    "ai_trading_source",
    "ai_trade_style",
    "ai_watch_zone_mode",
    "ai_eod_liquidate_time",
    "ai_entry_order_style",
    "ai_entry_confirm_max_slip_pct",
    "ai_entry_confirm_max_slip_px",
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
    "ai_position_slot_equity",
    "ai_max_buys_per_poll",
    "ai_max_sells_per_poll",
    "ai_risk_pct",
    "ai_size_from_free_equity",
    "ai_trade_style",
    "desk_product",
    "ai_h4_paper",
    "ai_h3_paper",
    "h4_min_price",
    "h4_min_dollar_vol",
    "h4_max_spread_pct",
    "h4_min_rs",
    "h4_hold_days",
    "h4_stop_pct",
    "h4_haircut_pct",
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
    "ai_watch_decision_max_age_sec",
    "ai_stale_data_flatten",
    "ai_stale_data_max_age_sec",
    "ai_premarket_working_sell",
    "ai_premarket_quote_max_age_sec",
    "ai_premarket_max_exit_slip_r",
    "ai_premarket_chase_step_sec",
    "ai_structure_ttl_sec",
    "ai_watch_expire_at_close",
    "ai_watch_seed_momentum",
    "ai_watch_seed_momentum_n",
    "ai_watch_seed_momentum_open",
    "ai_watch_seed_momentum_open_n",
    "ai_watch_seed_trending",
    "ai_watch_seed_trending_n",
    "ai_watch_seed_movers",
    "ai_watch_seed_movers_n",
    "ai_watch_movers_min_pct_change",
    "ai_watch_movers_enrich",
    "ai_movers_max_age_sec",
    "ai_watch_trending_min_pct_change",
    "ai_watch_trending_min_rvol",
    "ai_watch_seed_research",
    "ai_watch_seed_research_n",
    "ai_watch_seed_bb_live",
    "ai_watch_seed_bb_live_n",
    "ai_watch_bb_live_fresh_sec",
    "ai_watch_trending_min_score",
    "ai_watch_min_pct_change",
    "ai_watch_open_seed_min_pct",
    "ai_watch_open_seed_stream_min_pct",
    "ai_watch_min_rvol",
    "ai_watch_movers_min_rvol",
    "ai_watch_hot_move_rvol_waive_pct",
    "ai_watch_arm_min_rvol",
    "ai_watch_arm_rvol_sane_max",
    "ai_watch_look_max",
    "ai_watch_require_look_ext",
    "ai_watch_synth_zone_enabled",
    "ai_watch_zone_mode",
    "ai_watch_arm_mode",
    "ai_watch_require_db_zone",
    "ai_watch_armable_zone_kinds",
    "ai_edge_mode",
    "ai_exit_left_overbought",
    "ai_watch_exhaustion_rules",
    # Published so the book legend can state the live entry/exit criteria
    # instead of a fallback. The attempt cap and the dead-reentry pair are
    # entry gates the operator changed on 2026-09-01; be_at_r is the other
    # half of the breakeven floor (be_at_pct alone told half the story).
    "ai_watch_max_entries_per_symbol_day",
    "ai_watch_require_live_pctr",
    "ai_watch_require_realtime_macd",
    "ai_watch_arm_require_stream_price",
    "ai_local_trail_be_at_r",
    "ai_watch_exhaustion_live",
    "ai_watch_exhaustion_trade_price_only",
    "ai_watch_stream_bars_live",
    "ai_watch_arm_require_cm_rsi",
    "ai_watch_arm_cm_rsi_max",
    # Published so the book legend prints the band the gate is actually
    # using. Without it the legend silently falls back to a default and
    # stops describing the rule the moment the floor is changed.
    "ai_watch_arm_cm_rsi_min",
    "ai_watch_arm_cm_rsi_allow_falling_below",
    "ai_watch_require_realtime_rsi",
    "ai_watch_cm_rsi_local",
    "ai_watch_exhaustion_exit_sec",
    "ai_watch_exhaustion_exit_give_pct",
    "ai_watch_exhaustion_heat_min_pct",
    "ai_watch_exhaustion_heat_max_pct",
    "ai_watch_require_exh_rising",
    "ai_watch_soft_ob_enabled",
    "ai_watch_soft_ob_rsi_min",
    "ai_watch_mistimed_heat_enabled",
    "ai_watch_mistimed_heat_rsi_min",
    "ai_watch_mistimed_heat_rsi_peak_min",
    "ai_watch_ob_allow_hot",
    "ai_watch_in_zone_ignore_fade",
    "ai_watch_zone_exh_window_sec",
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
    "ai_spread_r_sane_max",
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
    "ai_local_trail_enabled",
    "ai_local_trail_arm_r",
    "ai_local_trail_give_r",
    "ai_local_trail_initial_give_r",
    "ai_local_trail_give_open_r",
    "ai_local_trail_tighten_mfe_r",
    "ai_local_trail_give_px",
    "ai_decision_ask_max_dev_pct",
    "ai_entry_limit_anchor",
    "ai_exit_min_hold_sec",
    "ai_exit_macd_curl_tighten",
    "ai_exit_macd_liquidate",
    "ai_exit_macd_hard_sell_sep",
    "ai_watch_ob_flat_min_pct",
    "ai_watch_arm_confirm_ticks",
    "macd_sep_mult",
    "macd_min_gap",
    "ai_watch_macd_max_age_sec",
    "ai_watch_macd_exh_override_min_pct",
    "ai_local_trail_be_at_pct",
    "ai_local_trail_give_max_pct",
    "ai_exit_macd_confirm_ticks",
    "ai_shelf_trace_sec",
    "ai_stranded_close_sec",
    "ai_exit_macd_liquidate_ignore_hold",
    "ai_exit_macd_curl_px",
    "ai_exit_macd_curl_on_falling",
    "ai_local_trail_give_spread_k",
    "ai_local_trail_give_spread_max_r",
    "ai_local_trail_be_at_spread_k",
    "ai_local_trail_min_give_px",
    "ai_local_trail_min_give_max_r",
    "ai_breakeven_offset_px",
    "ai_book_tick_sec",
    "ai_shelf_tick_sec",
    "ai_local_trail_damp_sec",
    "ai_fill_abort_r",
    "ai_runner_step_r",
    "ai_day_scalp_dual_tranche",
    "ai_dead_trade_min",
    "ai_dead_trade_mfe_r",
    "desk_product",
    "ai_h4_paper",
    "ai_h3_paper",
    "h4_min_price",
    "h4_min_dollar_vol",
    "h4_max_spread_pct",
    "h4_min_rs",
    "h4_hold_days",
    "h4_stop_pct",
    "h4_haircut_pct",
    "ai_late_hold_paper",
    "ai_late_hold_start",
    "ai_late_hold_end",
    "ai_late_hold_stop_pct",
    "ai_late_hold_dead_trade_min",
    "ai_dead_reentry_block",
    "ai_reentry_min_mfe_r",
    "ai_position_shadow_enabled",
    "ai_sell_signal_breakeven",
    "ai_heal_unprotected",
    "ai_broker_stop_enabled",
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
    "ai_watch_max_float_m",
    "ai_watch_admit_ticks",
    "ai_watch_admit_grace_sec",
    "ai_watch_stale_tape_drop_polls",
    "ai_watch_stale_timeout_sec",
    "ai_watch_stale_timeout_grace_sec",
    "ai_watch_stale_timeout_reseed_sec",
    "ai_watch_stale_timeout_include_need_stream",
    "ai_watch_stale_timeout_quiet_max_sec",
    "ai_watch_no_trade_after_subscribe_sec",
    "ai_watch_no_trade_reseed_sec",
    "ai_watch_admit_max_tape_age_sec",
    "ai_watch_movers_min_dollar_volume",
    "ai_watch_movers_min_price",
    "ai_watch_movers_admit_max_tape_age_sec",
    "ai_watch_max_stale_tape_seats",
    "ai_watch_stream_subscribe_grace_sec",
    "ai_watch_engine_push_max",
    "ai_watch_stream_enabled",
    "ai_watch_stream_max_age_sec",
    "ai_watch_stream_skip_margin_pct",
    "ai_max_position_pct",
    "ai_max_position_pct_cheap",
    "ai_position_slot_equity",
    "ai_watch_cheap_price",
    "ai_reentry_cooldown_sec",
    "ai_wash_cooldown_sec",
    "ai_entry_order_style",
    "ai_entry_limit_pad_pct",
    "ai_entry_marketable_pad_max_px",
    "ai_entry_limit_ttl_sec",
    "ai_entry_confirm_max_slip_pct",
    "ai_entry_confirm_max_slip_px",
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
    "movers_screener_enabled",
    "ai_movers_poll",
    "ai_movers_poll_idle",
    "ai_movers_top",
    "ai_movers_min_pct_change",
    "ai_movers_min_price",
    "ai_movers_max_price",
    "ai_movers_min_dollar_vol",
    "ai_movers_min_live_pct",
    "ai_movers_live_window_min",
    "ai_movers_min_minute_dollars",
    "ai_movers_live_min_open_minutes",
    "ai_movers_actives_top",
    "ai_movers_use_most_actives",
    "ai_movers_session_max",
    "ai_movers_session_append",
    "ai_movers_max_rows",
    "ai_movers_float_refresh_per_pass",
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
