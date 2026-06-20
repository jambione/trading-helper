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
