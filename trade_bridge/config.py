"""Bridge configuration — self-contained so the existing bot_config.json
plumbing stays untouched. Overrides live in config/trade_bridge.json.
"""
import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "trade_bridge.json"

DEFAULTS = {
    # "mock" | "alpaca" — use "alpaca" after scripts/alpaca_smoke.py passes
    "provider": "mock",

    # override either half of the provider pair independently
    "market_data_provider": "",
    "broker_provider": "",

    # hard server-side safety cap on order notional value (USD)
    "max_order_value": 1000.0,

    # stream rate pushed to clients (reads/sec)
    "stream_hz": 3.0,

    # max simultaneously monitored symbols (each runs its own engine task)
    "max_engines": 12,

    # keep an engine running for every momentum ticker (newest first,
    # capped by max_engines) so the list shows stances and monitors open warm
    "auto_watch": True,
    "auto_watch_interval": 15,

    # alpaca provider: same env keys as alpaca_trader
    "alpaca_api_key": os.getenv("ALPACA_API_KEY", ""),
    "alpaca_secret_key": os.getenv("ALPACA_SECRET_KEY", ""),
    "alpaca_paper": True,          # False only for live Mobile Trader orders
    "alpaca_poll_sec": 0.5,        # quote poll interval per symbol
    "alpaca_max_rps": 10.0,
    "alpaca_data_feed": "IEX",  # free tier only (SIP needs paid Alpaca data plan)

    # scripts/position_feed.py: seconds between broker position polls
    "position_poll_sec": 5.0,

    # ── signal thresholds ──
    "imbalance_buy": 1.8,
    "imbalance_sell": 0.55,
    "confirm_reads": 3,
    "max_spread_pct": 1.0,
    "wall_multiple": 4.0,
    "alert_cooldown": 30,
    "min_room_pct": 1.0,
    "momentum_reads": 6,
    "trend_window": 300,
    "max_downtrend_pct": 0.2,
    "long_window": 60,
    "long_confirm_secs": 20,
    "projection_minutes": 5.0,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg
