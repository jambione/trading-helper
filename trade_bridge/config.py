"""Bridge configuration — self-contained so the existing bot_config.json
plumbing stays untouched. Overrides live in config/trade_bridge.json.

Alpaca credentials resolve in this order (first non-empty wins per key):
  1. config/secrets.json  api_key / secret_key   ← the desk's source of truth
  2. process env  ALPACA_API_KEY / ALPACA_SECRET_KEY
  3. config/trade_bridge.json  alpaca_api_key / alpaca_secret_key

signal_engine.env no longer carries credentials — it holds engine settings.
"""
import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = _ROOT / "config" / "trade_bridge.json"
ENGINE_ENV_FILE = _ROOT / "signal_engine.env"
SECRETS_FILE = _ROOT / "config" / "secrets.json"

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
    "alpaca_api_key": "",
    "alpaca_secret_key": "",
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


def _parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines from an env file (comments / blanks ignored)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip().split("#")[0].strip().strip('"').strip("'")
            out[key] = value
    except Exception:
        pass
    return out


def _resolve_alpaca_credentials(file_cfg: dict) -> tuple[str, str]:
    """Fill alpaca keys from secrets.json → process env → trade_bridge.json.

    config/secrets.json is the desk's source of truth for credentials, so it
    is consulted first rather than as a last resort. The process env is still
    honoured after it, because a desk process has already had secrets.json
    overlaid onto its environment by desk_core.load_desk_env() — and a genuine
    shell export survives that overlay deliberately.
    """
    key = secret = ""
    try:
        if SECRETS_FILE.exists():
            secrets = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
            if isinstance(secrets, dict):
                key = (secrets.get("api_key") or "").strip()
                secret = (secrets.get("secret_key") or "").strip()
    except (OSError, ValueError):
        pass

    key = key or os.getenv("ALPACA_API_KEY", "").strip() \
        or (file_cfg.get("alpaca_api_key") or "").strip()
    secret = secret or os.getenv("ALPACA_SECRET_KEY", "").strip() \
        or (file_cfg.get("alpaca_secret_key") or "").strip()

    return key, secret


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    file_cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                file_cfg = loaded
                cfg.update(loaded)
        except Exception:
            pass

    # Credentials: resolve after file merge so missing json keys still pick
    # up signal_engine.env / secrets / process env. Explicit non-empty values
    # in trade_bridge.json (already in file_cfg) win via _resolve.
    key, secret = _resolve_alpaca_credentials(file_cfg)
    if key:
        cfg["alpaca_api_key"] = key
    if secret:
        cfg["alpaca_secret_key"] = secret
    return cfg
