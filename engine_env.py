"""
engine_env.py — safe read/update of signal_engine.env for the dashboard.

The dashboard's Engine panel edits a WHITELIST of signal_engine.env keys and
asks the engine to restart so they take effect. This module owns the file
surgery so it can be unit-tested without FastAPI.

Design rules:
  • Only keys in ENGINE_SAFE_KEYS can be read or written — API keys and
    credentials are never exposed through this path.
  • TRADER_MODE=live is REJECTED here. Going live is a deliberate, manual
    edit of signal_engine.env — it must never be one misclick away.
  • Comments and unrelated lines in the env file are preserved; an existing
    `KEY=` line is rewritten in place, otherwise the new line is appended to
    a dashboard-managed section at the end.
"""

from __future__ import annotations

import re
import tempfile
import os
from pathlib import Path

_HERE = Path(__file__).parent
ENGINE_ENV_FILE = _HERE / "signal_engine.env"

_MANAGED_HEADER = "# ── Dashboard-managed settings (Engine panel) ──"

# key → validator name (see _VALIDATORS below)
ENGINE_SAFE_KEYS: dict[str, str] = {
    # Mode
    "TRADER_MODE":            "trader_mode",
    "STRATEGY_MODE":          "strategy_mode",
    "REALTIME_BARS":          "flag01",
    "COMPARE_TICKERS":        "ticker_list",
    # Scalp exits / sizing
    "TRADE_AMOUNT":           "pos_float",
    "MAX_PRICE":              "pos_float",
    "MAX_TOTAL_EXPOSURE":     "pos_float",
    "STOP_LOSS":              "pos_float",
    "TAKE_PROFIT":            "pos_float",
    "MAX_HOLD_MINUTES":       "pos_int",
    "THREE_IND_MIN_HOLD":     "pos_int",
    # Risk guard
    "DAILY_LOSS_LIMIT":       "pos_float",
    "MAX_TRADES_PER_DAY":     "pos_int",
    "PDT_PROTECT":            "pdt_mode",
    # 3-indicator strategy parameters
    "THREE_IND_EXIT_MODE":          "exit_mode",
    "THREE_IND_REQUIRE_HOT":        "flag01",
    "THREE_IND_CM_RSI_LENGTH":      "pos_int",
    "THREE_IND_CM_RSI_BUY_MAX":     "pos_float",
    "THREE_IND_CM_RSI_SELL_MIN":    "pos_float",
    "THREE_IND_RTE_THRESHOLD":      "pos_int",
    "THREE_IND_RTE_SELL_FROM":      "any_float",
    "THREE_IND_MACD_FAST":          "pos_int",
    "THREE_IND_MACD_SLOW":          "pos_int",
    "THREE_IND_MACD_SIGNAL":        "pos_int",
    "THREE_IND_MACD_SEP_MULT":      "pos_float",
    "THREE_IND_MACD_SEP_WINDOW":    "pos_int",
    "THREE_IND_CONFIRM_WINDOW":     "pos_int",
    "THREE_IND_TREND_LOOKBACK":     "pos_int",
}

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _v_trader_mode(v: str) -> str:
    v = v.lower().strip()
    if v == "live":
        raise ValueError("TRADER_MODE=live cannot be set from the dashboard — "
                         "edit signal_engine.env manually to go live")
    if v not in ("off", "paper"):
        raise ValueError("TRADER_MODE must be off or paper")
    return v


def _v_strategy_mode(v: str) -> str:
    v = v.lower().strip()
    if v not in ("momentum", "three_indicator", "alert"):
        raise ValueError("STRATEGY_MODE must be momentum, three_indicator, or alert")
    return v


def _v_pdt_mode(v: str) -> str:
    v = v.lower().strip()
    if v not in ("warn", "block", "off"):
        raise ValueError("PDT_PROTECT must be warn, block, or off")
    return v


def _v_exit_mode(v: str) -> str:
    v = v.lower().strip()
    if v not in ("any", "all"):
        raise ValueError("exit mode must be any or all")
    return v


def _v_flag01(v: str) -> str:
    v = str(v).lower().strip()
    if v in ("1", "true", "yes", "on"):
        return "1"
    if v in ("0", "false", "no", "off", ""):
        return "0"
    raise ValueError("must be 0 or 1")


def _v_ticker_list(v: str) -> str:
    syms = [s.strip().upper() for s in str(v).split(",") if s.strip()]
    for s in syms:
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker symbol: {s!r}")
    return ",".join(syms)


def _v_pos_float(v: str) -> str:
    f = float(v)
    if f < 0:
        raise ValueError("must be ≥ 0")
    return f"{f:g}"


def _v_any_float(v: str) -> str:
    return f"{float(v):g}"


def _v_pos_int(v: str) -> str:
    i = int(float(v))
    if i < 0:
        raise ValueError("must be ≥ 0")
    return str(i)


_VALIDATORS = {
    "trader_mode":   _v_trader_mode,
    "strategy_mode": _v_strategy_mode,
    "pdt_mode":      _v_pdt_mode,
    "exit_mode":     _v_exit_mode,
    "flag01":        _v_flag01,
    "ticker_list":   _v_ticker_list,
    "pos_float":     _v_pos_float,
    "any_float":     _v_any_float,
    "pos_int":       _v_pos_int,
}


def validate(key: str, value) -> str:
    """Normalize+validate one value. Raises ValueError on bad key or value."""
    if key not in ENGINE_SAFE_KEYS:
        raise ValueError(f"{key} is not an editable engine setting")
    try:
        return _VALIDATORS[ENGINE_SAFE_KEYS[key]](str(value))
    except ValueError as e:
        raise ValueError(f"{key}: {e}") from None


def read_engine_env(path: Path | None = None) -> dict:
    """
    Current ACTIVE values for whitelisted keys (None when unset/commented).
    Trailing inline comments are stripped from values.
    """
    # Resolved at call time (not def time) so tests can monkeypatch ENGINE_ENV_FILE
    path = path or ENGINE_ENV_FILE
    values: dict = {k: None for k in ENGINE_SAFE_KEYS}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in ENGINE_SAFE_KEYS:
            values[key] = value.split("#", 1)[0].strip()
    return values


def update_engine_env(updates: dict, path: Path | None = None) -> list:
    """
    Validate and apply `updates` ({key: value}) to the env file atomically.
    Returns the list of keys actually written. Raises ValueError on the first
    invalid key/value (nothing is written in that case).
    """
    path  = path or ENGINE_ENV_FILE
    clean = {k: validate(k, v) for k, v in updates.items()}
    if not clean:
        return []

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(clean)

    out = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(raw)

    if remaining:
        if _MANAGED_HEADER not in lines:
            out += ["", _MANAGED_HEADER]
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        Path(tmp_path).replace(path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return list(clean.keys())
