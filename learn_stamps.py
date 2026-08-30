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
    "desk_product",
    "ai_h4_paper",
    "ai_h3_paper",
    "ai_exit_left_overbought",
    "ai_exit_macd_liquidate",
    "ai_exit_macd_hard_sell_sep",
    "ai_exit_macd_confirm_ticks",
    "ai_watch_max_entries_per_symbol_day",
    "ai_exit_macd_liquidate_ignore_hold",
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
    # Which entry rule is live. ai_watch_tv_exh_rsi routes exhaustion_allows_buy
    # to the two-%R-lines-then-CM-RSI-2 test or to the legacy heat path, and
    # those are different strategies — a ledger that cannot tell them apart
    # pools them into one meaningless mean.
    "ai_watch_tv_exh_rsi",
    "ai_watch_min_price",
    # The whole local trail. These decide when the shelf starts tracking, how
    # far behind it sits, and when it stops going under the fill — which is to
    # say they decide what every trade banks. Eight of the twelve knobs changed
    # on 2026-08-19 were not fingerprinted, so that day's rows would have
    # claimed the same regime as the day before it.
    "ai_local_trail_arm_r",
    "ai_local_trail_arm_pct",
    "ai_local_trail_be_at_r",
    "ai_local_trail_be_at_pct",
    "ai_local_trail_give_r",
    # Sets the shelf at fill independently of the trail. It changes which
    # trades survive their first minute, so two settings must not stamp
    # the same config_fp — outcomes could not be attributed to it.
    "ai_local_trail_initial_give_r",
    "ai_local_trail_give_open_r",
    "ai_local_trail_give_max_pct",
    # The spread-relative shelf and its cap. Both decide the width of the
    # cushion a fill is protected by, which is to say what every trade
    # banks — the same argument as the twelve knobs above.
    "ai_local_trail_give_spread_k",
    "ai_local_trail_give_spread_max_r",
    "ai_local_trail_be_at_spread_k",
    # How long the desk's discretionary exits stay holstered. This is the
    # single largest change to what a fill returns that this desk has ever
    # made: it moves max loss per position from ~0.10R to the 1R stop. Rows
    # from a min-hold session must not claim the same regime as rows from a
    # session where the shelf fired on the first tick.
    "ai_exit_min_hold_sec",
    # Which lever opens a position. The 8/26 redesign moved entries from %R
    # exhaustion + Connors RSI-2 to a MACD bullish gap, which is a different
    # strategy — none of these were fingerprinted, so the ledger would pool
    # MACD sessions and EXH/RSI sessions into one meaningless mean, exactly
    # the failure ai_watch_tv_exh_rsi is on this list to prevent.
    "ai_watch_arm_require_macd",
    "macd_min_gap",
    "macd_sep_mult",
    "macd_require_cross",
    # Whether a CLOSING gap can still open a position. Size and direction are
    # different rules; a session that refuses the fade is not the same regime
    # as one that buys it.
    "ai_watch_macd_block_narrowing",
    # Whether EXH confluence can override the gap-size tests. A session
    # that arms on confluence is not the same regime as one that
    # requires 0.8x separation.
    "ai_watch_macd_exh_override",
    "ai_watch_macd_exh_override_min_pct",
    # Whether the lever must come off the live tape. A session that armed
    # on REST-fallback MACD is not the same regime as one that did not.
    "ai_watch_require_realtime_macd",
    "ai_watch_macd_max_age_sec",
    # And whether a curl back to bearish pulls the shelf under the print.
    # This changes what the exit IS, not merely how wide it sits.
    "ai_exit_macd_curl_tighten",
    "ai_exit_macd_curl_px",
    "ai_exit_macd_curl_on_falling",
    # Which names are admitted at all. min_pct_change gates only the
    # big-mover seed; this one gates the soft open seed, where most
    # admissions actually come from.
    "ai_watch_open_seed_min_pct",
    # The rest of the shelf. Found by tools/setup_audit.py 2026-08-22, which
    # is the point of having it: these were all missing while the file above
    # was already documenting that eight unfingerprinted knobs once made a
    # day claim the previous day's regime. Every one of them decides how
    # wide the cushion is or when it tightens.
    "ai_local_trail_give_px",
    "ai_local_trail_min_give_px",
    "ai_local_trail_min_give_max_r",
    "ai_local_trail_tighten_mfe_r",
    "ai_local_trail_damp_sec",
    "ai_local_trail_print_ring",
    # Dead-trade is a time-stop, so it decides holds the same way the
    # min-hold gate does — and the two now interact.
    "ai_dead_trade_min",
    "ai_dead_trade_mfe_r",
    # Entry gates and sizing.
    "ai_max_spread_r",
    "ai_max_position_pct_cheap",
    "ai_watch_min_adx",
    "ai_watch_min_proximity",
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
        "desk_product": str(data.get("desk_product") or "") or None,
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
