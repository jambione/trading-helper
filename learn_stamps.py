"""Regime stamps for the learning loop.

Every row that later becomes a hypothesis (outcomes, trades, shadow, rejects)
must carry *which rules were live* when the decision was made. Without that,
hybrid vs continuation days collapse into one mean and the forward-test ledger
cannot prove yesterday's lesson.

Stamps are small, denormalized, and cheap: git version is cached; the config
fingerprint is rehashed only when bot_config.json's mtime changes.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_BOT_CONFIG = ROOT / "config" / "bot_config.json"

# Knobs that change *what* the desk buys/sells/sizes — not credentials, not UI.
_FINGERPRINT_KEYS = (
    "ai_edge_mode",
    "ai_exit_left_overbought",
    "ai_entry_broker_target",
    "ai_watch_exhaustion_heat_min_pct",
    "ai_watch_min_rvol",
    "ai_watch_min_pct_change",
    "ai_watch_require_exhaustion_data",
    "ai_risk_pct",
    "ai_daily_loss_limit_r",
    "ai_max_open_risk_pct",
    "ai_max_positions",
    "ai_position_slot_equity",
    "ai_max_buys_per_poll",
    "ai_fill_abort_r",
    "ai_min_reward_risk",
    "ai_max_position_pct",
    "ai_watch_min_stop_pct",
    "require_protective_exit",
    "ai_stop_use_market",
    "ai_pdt_protect",
    "ai_heal_unprotected",
    "paper",
    "ai_trading_source",
)

_fp_cache: tuple[float, str] | None = None  # (mtime, hex)
_stamp_cache: tuple[float, dict[str, Any]] | None = None
_STAMP_TTL_SEC = 30.0


def _load_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(cfg, dict):
        return cfg
    try:
        return json.loads(_BOT_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def config_fingerprint(cfg: dict[str, Any] | None = None) -> str:
    """Stable short hash of the risk/edge knobs (not the full secrets file)."""
    global _fp_cache
    if cfg is None:
        try:
            mtime = _BOT_CONFIG.stat().st_mtime
        except OSError:
            mtime = -1.0
        if _fp_cache is not None and _fp_cache[0] == mtime:
            return _fp_cache[1]
        data = _load_cfg(None)
        payload = {k: data.get(k) for k in _FINGERPRINT_KEYS}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        _fp_cache = (mtime, digest)
        return digest

    payload = {k: cfg.get(k) for k in _FINGERPRINT_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]


def git_version() -> str:
    try:
        from version import get_version
        return str(get_version() or "unknown")
    except Exception:
        return "unknown"


def regime_stamp(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fields to merge into outcomes / trades / shadow / rejects.

    Cached briefly so 10k shadow rows/day do not re-read config every poll.
    Pass *cfg* to bypass cache (tests).
    """
    global _stamp_cache
    now = time.time()
    if cfg is None and _stamp_cache is not None:
        ts, cached = _stamp_cache
        if now - ts < _STAMP_TTL_SEC:
            return dict(cached)

    data = _load_cfg(cfg)
    stamp = {
        "edge_mode": str(data.get("ai_edge_mode") or "") or None,
        "exit_left_overbought": bool(data.get("ai_exit_left_overbought", False)),
        "git_version": git_version(),
        "config_fp": config_fingerprint(data if cfg is not None else None),
        "paper": bool(data.get("paper", True)),
        "book_owner": str(data.get("ai_trading_source") or "") or None,
    }
    if cfg is None:
        _stamp_cache = (now, stamp)
    return dict(stamp)


def merge_regime(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return *row* with regime fields filled only where missing."""
    stamp = regime_stamp(cfg)
    out = dict(row)
    for k, v in stamp.items():
        if out.get(k) is None or out.get(k) == "":
            out[k] = v
    return out


def clear_caches() -> None:
    """Test helper."""
    global _fp_cache, _stamp_cache
    _fp_cache = None
    _stamp_cache = None
    try:
        from version import get_version
        get_version.cache_clear()
    except Exception:
        pass
