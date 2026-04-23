import json
import os
import tempfile
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "bot_config.json"

DEFAULT_CONFIG = {
    # ── API credentials ──────────────────────────────────────
    "api_key":     os.getenv("ALPACA_API_KEY", ""),
    "secret_key":  os.getenv("ALPACA_SECRET_KEY", ""),
    "finnhub_key": os.getenv("FINNHUB_API_KEY", ""),

    # ── Data fetching ────────────────────────────────────────
    "bar_timeframe":    "5Min",   # 1Min | 5Min | 15Min | 1Hour | 1Day
    "bar_count":        300,
    "scan_interval_sec": 60,

    # ── Ticker source ────────────────────────────────────────
    "ticker_log_file": "transcription/ticker_log.csv",

    # ── Signal: %R Trend Exhaustion ─────────────────────────
    "rte_threshold":  20,    # overbought/oversold zone edge (0-50)
    "rte_min_boxes":   2,    # consecutive bars required to be "on deck"
    "rte_side":      "red",  # "red" = overbought watch

    # ── Signal: CM RSI-2 ────────────────────────────────────
    "rmi_ma_slow":   200,    # slow MA for price-above-trend filter

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
    "tv_chart_url": "https://www.tradingview.com/chart/",

    # Transcription
    "device_index": None,
}

# Keys the dashboard API is allowed to update
SAFE_CONFIG_KEYS = [
    "api_key", "secret_key", "finnhub_key",
    "bar_timeframe", "bar_count", "scan_interval_sec",
    "ticker_log_file",
    "rte_threshold", "rte_min_boxes", "rte_side",
    "rmi_ma_slow",
    "obv_length",
    "macd_fast", "macd_slow", "macd_signal",
    "volume_surge_mult",
    "ema_short", "ema_long",
    "wr_length",
    "tv_chart_url",
    "device_index",
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
    return cfg


def save_config(cfg: dict):
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=CONFIG_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            Path(tmp_path).replace(CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        print(f"[CFG] Failed to save config: {e}")
