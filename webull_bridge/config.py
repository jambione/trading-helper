"""Bridge configuration — self-contained so the existing bot_config.json
plumbing stays untouched. Overrides live in config/webull_bridge.json.

Signal thresholds default to the same values as webull-l2/config.json so
the phone shows the stance the desktop monitor would.
"""
import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "webull_bridge.json"

DEFAULTS = {
    # "mock" until Webull OpenAPI credentials are approved, then "webull"
    "provider": "mock",

    # override either half of the provider pair independently, e.g.
    # broker_provider "webull" (real positions/orders) while
    # market_data_provider stays "mock" because the account has no
    # OpenAPI Advanced Quotes subscription for L2 depth
    "market_data_provider": "",
    "broker_provider": "",

    # hard server-side safety cap on order notional value (USD)
    "max_order_value": 1000.0,

    # depth stream rate pushed to clients (reads/sec)
    "stream_hz": 3.0,

    # max simultaneously monitored symbols (each runs its own engine task)
    "max_engines": 12,

    # keep an engine running for every momentum ticker (newest first,
    # capped by max_engines) so the list shows stances and monitors open warm
    "auto_watch": True,
    "auto_watch_interval": 15,

    # webull provider: total depth-poll budget across all engines (req/sec);
    # per-symbol interval stretches automatically as engines are added
    "webull_max_rps": 6.0,

    # scripts/position_feed.py: seconds between broker position polls
    "position_poll_sec": 5.0,

    # ── signal thresholds (mirror webull-l2/config.json) ──
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

    # ── Webull OpenAPI credentials (used when provider == "webull") ──
    "webull_app_key": os.getenv("WEBULL_APP_KEY", ""),
    "webull_app_secret": os.getenv("WEBULL_APP_SECRET", ""),
    "webull_account_id": os.getenv("WEBULL_ACCOUNT_ID", ""),
    "webull_region": os.getenv("WEBULL_REGION", "us"),
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg
