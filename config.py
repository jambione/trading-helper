import json
import os
import tempfile
from pathlib import Path

CONFIG_FILE  = Path(__file__).parent / "config" / "bot_config.json"
SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"
SECRETS_KEYS = ["api_key", "secret_key", "finnhub_key",
                "push_vapid_private_key", "push_contact_email"]

DEFAULT_CONFIG = {
    # ── API credentials ──────────────────────────────────────
    "api_key":     os.getenv("ALPACA_API_KEY", ""),
    "secret_key":  os.getenv("ALPACA_SECRET_KEY", ""),
    "finnhub_key": os.getenv("FINNHUB_API_KEY", ""),

    # ── Data fetching ────────────────────────────────────────
    "bar_timeframe":    "1Min",   # 1Min | 5Min | 15Min | 1Hour | 1Day
    "bar_count":        300,
    "scan_interval_sec": 60,

    # ── Ticker source ────────────────────────────────────────
    "ticker_log_file": "transcription/ticker_log.csv",

    # ── Signal: %R Trend Exhaustion ─────────────────────────
    "rte_threshold":  20,    # overbought/oversold zone edge (0-50)
    "rte_min_boxes":   2,    # consecutive bars required to be "on deck"
    "rte_side":      "red",  # "red" = overbought watch

    # ── Signal: CM RSI-2 ────────────────────────────────────
    "cm_rsi_length":    2,   # RSI period (2 = original Larry Connors CM RSI-2)
    "cm_rsi_oversold": 25,   # approaching-oversold threshold for signal
    "rmi_ma_slow":    200,   # slow MA for price-above-trend filter

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

    # ── Claude trader (claude_trader.py) ──────────────────────────────────────
    # Research runs the Claude Code CLI on a fixed ET schedule and publishes
    # ranked ideas to claude_suggestions.json. When claude_trading_enabled is
    # also true, qualifying ideas become real Alpaca bracket orders and
    # claude_positions.manage_open_positions() enforces the stop/scale-out/
    # trailing/time rules mechanically — only entry and thesis-break use Claude.
    #
    # These are the SERVER's copy of these settings. The monitor's
    # momentum_config.json keeps only display tuning; it no longer trades.
    "claude_trader_enabled":     False,   # launch claude_trader.py from start_all
    "claude_research_enabled":   False,   # run the scheduled research prompt
    "claude_trading_enabled":    False,   # place real orders off that research
    "claude_backend":       "claude_cli",
    "claude_cli_bin":          "claude",
    "claude_model":            "sonnet",
    "claude_effort":            "xhigh",  # low|medium|high|xhigh|max
    # Fixed ET run times beat interval polling: most of a run's cost is search
    # fees, and off-hours searches re-derive the same macro on a closed tape.
    "claude_research_times": ["04:00", "11:00", "13:00"],
    "claude_research_weekdays_only": True,
    "claude_research_catchup_min": 120,   # how late a missed slot may still fire
    "claude_prompt_file": "claude_prompt.txt",
    "claude_request_timeout":   600.0,
    "claude_live_search":        True,
    "claude_search_tools":      "web",
    "claude_max_turns":             8,
    "claude_max_output_tokens": 10000,
    "claude_use_prior_context":  True,
    "claude_save_reports":       True,
    "claude_max_price":         100.0,    # prompt prefers names under this
    "claude_quote_poll":         15.0,    # re-quote published rows
    "claude_volume_poll":        60.0,    # re-sum today's minute bars for RVOL
    "claude_avg_days":             10,    # sessions in the RVOL denominator
    "claude_rvol_time_adjusted": True,
    "claude_trade_amount":     1000.0,
    "claude_max_positions":         5,
    "claude_max_buys_per_poll":     3,
    "claude_max_sells_per_poll":    5,
    "claude_risk_pct":            1.0,    # max % of account risked per trade
    "claude_trade_style": "Moderate position",
    "claude_min_reward_risk":     3.0,    # reject entries below this R:R
    "claude_positions_poll_sec":  5.0,    # mechanical manage_open_positions tick

    # ── Trending screener (trending_screener.py) ──────────────────────────────
    # Stocktwits carries no usable quotes, so rows are enriched from Alpaca.
    # LOOK badges are NOT computed here — those thresholds are desk display
    # settings and stay in the monitor's momentum_config.json.
    "trending_screener_enabled": False,   # launch trending_screener.py
    "stocktwits_poll":           60.0,    # seconds between Stocktwits polls
    "stocktwits_quote_poll":     15.0,
    "stocktwits_volume_poll":    60.0,
    "stocktwits_stocks_only":    True,
    "stocktwits_avg_days":         10,
    "stocktwits_rvol_time_adjusted": True,
}

# Keys the dashboard API is allowed to update
SAFE_CONFIG_KEYS = [
    "api_key", "secret_key", "finnhub_key",
    "bar_timeframe", "bar_count", "scan_interval_sec",
    "ticker_log_file",
    "rte_threshold", "rte_min_boxes", "rte_side",
    "cm_rsi_length", "cm_rsi_oversold",
    "rmi_ma_slow",
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
    "claude_trader_enabled",
    "claude_research_enabled",
    "claude_trading_enabled",
    "claude_backend",
    "claude_cli_bin",
    "claude_model",
    "claude_effort",
    "claude_research_times",
    "claude_research_weekdays_only",
    "claude_research_catchup_min",
    "claude_prompt_file",
    "claude_request_timeout",
    "claude_live_search",
    "claude_search_tools",
    "claude_max_turns",
    "claude_max_output_tokens",
    "claude_use_prior_context",
    "claude_save_reports",
    "claude_max_price",
    "claude_quote_poll",
    "claude_volume_poll",
    "claude_avg_days",
    "claude_rvol_time_adjusted",
    "claude_trade_amount",
    "claude_max_positions",
    "claude_max_buys_per_poll",
    "claude_max_sells_per_poll",
    "claude_risk_pct",
    "claude_trade_style",
    "claude_min_reward_risk",
    "claude_positions_poll_sec",
    "trending_screener_enabled",
    "stocktwits_poll",
    "stocktwits_quote_poll",
    "stocktwits_volume_poll",
    "stocktwits_stocks_only",
    "stocktwits_avg_days",
    "stocktwits_rvol_time_adjusted",
]


def load_config() -> dict:
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
    return cfg


def _write_json(path: Path, data: dict):
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
            raise
        Path(tmp_path).replace(path)
        tmp_path = None   # rename succeeded — nothing to clean up
    except Exception as e:
        print(f"[CFG] Failed to save {path.name}: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def save_config(cfg: dict):
    secrets  = {k: cfg[k] for k in SECRETS_KEYS if k in cfg and cfg[k]}
    main_cfg = {k: v for k, v in cfg.items() if k not in SECRETS_KEYS}
    _write_json(CONFIG_FILE, main_cfg)
    if secrets:
        _write_json(SECRETS_FILE, secrets)
